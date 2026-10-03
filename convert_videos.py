#!/usr/bin/env python3
"""
Batch-convert .mp4 and .mkv files in a folder (recursively) to H.265/HEVC using ffmpeg,
running software encoding. Files already encoded in H.265 are copied as-is.

Usage:
    python convert_videos.py [-i input_folder] [-o output_folder] [--crf 22] [--duration -1]

If -i/-o are omitted they default to the current folder, but the input and output
folders must be different and neither may be nested inside the other, so at least one
of -i/-o must be given explicitly.

The script never modifies anything in the input path: it only reads source files
and writes to the output folder (including its log). Source files are always
preserved, so it runs fine on a read-only input path.

Requires: ffmpeg and ffprobe available on PATH.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any


def _enable_windows_ansi_support() -> None:
    """On Windows, ANSI/VT escape sequences are only rendered as colors if
    ENABLE_VIRTUAL_TERMINAL_PROCESSING is turned on for the relevant console
    output handle. Modern Windows Terminal (the Windows 11 default for both
    PowerShell and Command Prompt) already enables this, but a plain conhost
    session or some embedded terminals might not, in which case escape codes
    would print as literal text instead of coloring anything. This turns it on
    explicitly for both stdout and stderr, so colors work regardless of which
    console is in front. No-op elsewhere, and never raises — if it can't enable
    the mode for either handle, colors simply won't render there, but
    everything else still works."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # windll exists only on Windows
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        for std_handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(std_handle)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass


_enable_windows_ansi_support()

# Warnings/errors are logged to the console (in addition to the log file) in
# color, so they stand out from the plain per-file progress line. Colors are
# only enabled when stderr is a real terminal, so piping/redirecting output (or
# the log file, which uses a separate plain-text handler) never ends up with
# raw escape codes.
_SUPPORTS_COLOR: bool = sys.stderr.isatty()
COLOR_WARNING: str = "\033[93m" if _SUPPORTS_COLOR else ""  # yellow
COLOR_ERROR: str = "\033[91m" if _SUPPORTS_COLOR else ""    # red
COLOR_RESET: str = "\033[0m" if _SUPPORTS_COLOR else ""


class ConversionError(Exception):
    """Raised when ffprobe or ffmpeg fails for a given file."""
    def __init__(self, file: Path, reason: str) -> None:
        self.file = file
        self.reason = reason
        super().__init__(f"{file}: {reason}")


class ConversionTimeoutError(ConversionError):
    """Raised when an ffprobe subprocess exceeds its allotted timeout. Logged more
    loudly than a plain ConversionError, but handled the same way: the file is skipped
    and the batch continues. Aborting the whole run on a single stuck probe costs far
    more than it saves on a job measured in days, and the systemic fault that would
    justify stopping (the source drive dropping offline) is caught by the consecutive-
    failure circuit breaker instead — see CONSECUTIVE_FAILURE_LIMIT. Note that ffmpeg
    encode/decode passes are not bounded by a wall-clock timeout at all; they're
    supervised by the liveness-based stall watchdog — see run_ffmpeg_with_watchdog."""
    pass


# Minimum timeout for any single ffprobe call, regardless of file size, so very small
# files still get a sane floor rather than a near-zero allowance. A metadata probe of
# a healthy file completes in tens of milliseconds, so a 60s floor is already ~1000x
# normal — generous enough to absorb a cold cache and a spun-down disk, while still
# firing inside a useful window rather than hours later.
TIMEOUT_FLOOR_SECONDS: int = 60  # 1 minute

# Small additional allowance per GB of source file size. ffprobe reads bounded
# container metadata (headers and index), NOT the file body, so its runtime is very
# nearly independent of file size — measured, a ~200x size increase costs well under
# 2x the probe time. This term therefore exists only to cover seek latency on large
# files over network mounts or spinning disks, not to scale with the data volume.
TIMEOUT_SECONDS_PER_GB: int = 5  # 5 seconds per GB

# Hard ceiling on any single probe timeout. Without this, the per-GB term would hand a
# large remux a multi-hour allowance, which in practice means the timeout never fires
# within a useful window — the failure mode is a run that looks hung for most of a day
# before reporting anything.
TIMEOUT_CEILING_SECONDS: int = 10 * 60  # 10 minutes

# --- Encode stall watchdog -------------------------------------------------------
# ffmpeg encode/decode passes are guarded by liveness, not by elapsed time. A wall
# clock cap can't work here: a legitimate veryslow encode of a large file can run for
# many hours, and any cap generous enough to never false-positive is too generous to
# catch a real hang. Instead the watchdog treats a process as alive if EITHER it is
# still emitting -progress heartbeats OR its consumed CPU time is still climbing, and
# kills it only when BOTH have been flat for this long. Elapsed time never enters into
# it, so an arbitrarily slow-but-healthy encode is safe indefinitely.
#
# The CPU-time signal is what makes this safe. ffmpeg goes genuinely silent on the
# -progress stream during the tail of an encode, while the encoder's lookahead buffer
# drains and the container index is written — measured at ~35s of total silence even
# on a small test file, and it scales with preset and file size. During that window
# the process is pegged at ~100% CPU. A real hang (a wedged Quick Sync session, a
# blocked read on a dropped mount) is blocked in a driver or I/O call and consumes no
# CPU at all, which cleanly separates the two cases.
ENCODE_STALL_SECONDS: int = 15 * 60  # 15 minutes of no progress AND no CPU movement

# How often the watchdog samples CPU time while waiting for the next heartbeat.
ENCODE_STALL_POLL_SECONDS: float = 5.0

# CPU time must advance by at least this much between samples to count as movement,
# so scheduler noise and accounting granularity don't read as liveness on a truly
# wedged process.
ENCODE_STALL_MIN_CPU_DELTA: float = 0.10  # seconds

# --- Interlacing detection -------------------------------------------------------
# Deinterlacing is decided per file from the picture content, not from the container's
# field_order tag, which is frequently absent, wrong, or stale on DVD rips. Detection
# decodes a short sample through ffmpeg's idet filter and reads its frame counts.
#
# The distinction that matters is telecine vs true interlace, because the correct fix
# differs and guessing wrong damages the picture:
#   * Film-sourced NTSC DVDs are 23.976p carried as 29.97i via 3:2 pulldown. The fix
#     is inverse telecine (fieldmatch + decimate), which reconstructs the original
#     progressive frames exactly and drops the frame count by 20% — better quality AND
#     a smaller file.
#   * Video-sourced content (TV, concerts, extras) is genuinely 29.97i with unique
#     motion in every field. Nothing can reconstruct whole frames, so it must be
#     interpolated with a real deinterlacer (bwdif).
#   * Much PAL and soft-telecined NTSC content is already progressive despite an
#     interlaced flag, and must be left completely alone.
# Applying a deinterlacer to progressive content softens it permanently; applying
# decimate to true video content throws away every fifth frame of real motion.

# How many frames of the sample to run through idet. A few hundred is enough for a
# stable ratio while keeping detection to a second or two even on 1080p.
IDET_SAMPLE_FRAMES: int = 400

# How far into the file to start the sample, as a fraction of its duration. Opening
# credits, studio logos and fades are often progressive (or black) even in interlaced
# content, so sampling from the very start misclassifies a lot of discs.
IDET_SAMPLE_POSITION: float = 0.35

# Fraction of ALL sampled frames (undetermined included) that must show comb before a
# file is treated as needing any filtering. Compression noise makes idet flag the
# occasional frame in genuinely progressive content, so this sits well above zero.
IDET_INTERLACED_THRESHOLD: float = 0.20

# Minimum frames that must come back from a sample for its ratios to mean anything.
# A truncated or unseekable sample yielding a handful of frames is reported as
# 'unknown' (leave the file alone) rather than classified from near-zero evidence.
IDET_MIN_SAMPLE_FRAMES: int = 50

# Fraction of sampled frames carrying a repeated field above which the source is taken
# to be 3:2 pulldown. Telecine duplicates one field every five frames, so a clean
# cadence lands around 20-40%; genuine interlace and progressive content sit at ~0%.
IDET_REPEAT_FIELD_THRESHOLD: float = 0.12

# Alternative telecine signal: the fraction by which a fieldmatch pass must cut the
# combed-frame ratio. Field matching re-pairs fields into whole frames, which only
# works on a pulldown cadence — measured on clean telecine it takes a ~100% combed
# sample to 0%, while true interlace barely moves.
IDET_FIELDMATCH_RECOVERY: float = 0.70

# Frame rates (fps) at which 3:2 pulldown is possible at all — NTSC 29.97 and its
# 30.0 variant. A 25fps PAL source flagged interlaced is never telecined in this
# sense, so it goes down the deinterlace path without the fieldmatch test.
NTSC_FRAME_RATES: tuple[float, ...] = (29.97, 30.0)
NTSC_FRAME_RATE_TOLERANCE: float = 0.5

# Frame rates that are inherently progressive. Interlaced video only exists in the
# broadcast formats built around it (25i/50i for PAL, 29.97i/59.94i for NTSC); no
# interlaced format runs at film rate. So a file already at 23.976/24fps cannot be
# interlaced video, whatever comb idet thinks it sees — and idet DOES produce false
# positives here, because very sharp horizontal detail looks like comb to it (measured:
# a genuinely progressive 24p test pattern reported 63% of frames combed). Trusting
# that would apply a deinterlacer to clean progressive video and soften it permanently,
# so film-rate sources short-circuit to 'progressive'.
PROGRESSIVE_FRAME_RATES: tuple[float, ...] = (23.976, 24.0)
PROGRESSIVE_FRAME_RATE_TOLERANCE: float = 0.1

# Filter chains for each outcome.
# fieldmatch reconstructs progressive frames from the 3:2 cadence; the yadif between
# handles the occasional orphaned combed frame fieldmatch can't pair up (a bad edit or
# a cadence break); decimate then drops the duplicated fifth frame, taking 29.97 back
# to 23.976. bwdif is used for true interlace in send_frame mode, which outputs one
# frame per input frame rather than doubling the rate — doubling would undo much of
# the size reduction this script exists to achieve.
DETELECINE_FILTER: str = "fieldmatch=order=auto:combmatch=full,yadif=deint=interlaced,decimate"
DEINTERLACE_FILTER: str = "bwdif=mode=send_frame:parity=auto:deint=all"


# A single bad file shouldn't end a multi-day batch, so per-file failures (including
# probe timeouts) skip the file and carry on. But a systemic fault — the source drive
# dropping offline, ffmpeg going missing — would otherwise churn through thousands of
# files failing identically. Aborting after this many CONSECUTIVE failures catches the
# systemic case while leaving isolated bad files as mere skips. The counter resets on
# any successful file.
CONSECUTIVE_FAILURE_LIMIT: int = 10

# --- Audio ----------------------------------------------------------------------
# Every kept audio track is re-encoded to stereo in one of these codecs. AAC is the
# default for compatibility: it plays essentially everywhere (smart-TV built-in
# players, streaming boxes, phones, browsers) and in both MKV and MP4, whereas Opus is
# missing from many TV players and from Roku's MP4 support entirely. Opus is the more
# efficient codec, so AAC gets a higher bitrate to land at roughly comparable quality;
# the extra 32 kbps costs ~29 MB over a two-hour film. Note that standard ffmpeg builds
# use ffmpeg's own 'aac' encoder (the better libfdk_aac is excluded for licensing
# reasons), which is solid at 160 kbps but weaker at low bitrates — so don't lower it.
AUDIO_CODEC_SETTINGS: dict[str, tuple[str, str]] = {
    "aac": ("aac", "160k"),       # (ffmpeg encoder, bitrate)
    "opus": ("libopus", "128k"),
}
DEFAULT_AUDIO_CODEC: str = "aac"

# Cap on each informational subtitle-size probe. This statistic is a nicety, so a
# pathological file must not hold up the batch: on timeout the figure is simply
# omitted for that file and the conversion is unaffected.
SUBTITLE_MEASURE_TIMEOUT_SECONDS: float = 300.0

# Hardware (Quick Sync) encodes launched back-to-back can occasionally hit a
# transient session/driver hiccup that a bare re-run of the same command doesn't
# reproduce (e.g. the GPU context from the previous file not being fully released
# yet). A couple of automatic retries with a short pause absorbs that without
# masking a genuinely bad file, which will fail the same way on every attempt.
# Software (libx265) encode failures aren't retried: they're far more likely to
# indicate a real problem, and retrying would be costly given how much slower a
# software encode is.
HARDWARE_ENCODE_MAX_ATTEMPTS: int = 3
HARDWARE_ENCODE_RETRY_DELAY_SECONDS: int = 5

# Valid -preset values differ by encoder: hevc_qsv (hardware) maps preset names onto
# Intel's numeric TargetUsage scale, running from "veryfast" to "veryslow"; libx265
# (software) has the x264/x265-style range from "ultrafast" to "veryslow" (x265 also
# defines "placebo", but it's deliberately excluded here — negligible gains for a huge
# time cost). These roughly bracket each encoder's default before our own defaults
# below, which favor quality over speed for both.
QSV_PRESETS: tuple[str, ...] = ("veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
X265_PRESETS: tuple[str, ...] = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
                                 "slow", "slower", "veryslow")

# Our defaults when --preset isn't given: veryslow for hardware (paired with
# look_ahead, this maximizes quality-per-bit on QSV — see build_ffmpeg_cmd) and slow
# for software (a common sweet spot; veryslow's software gains are usually small
# relative to the extra time — see xcodecpack.com's HEVC settings guide).
DEFAULT_QSV_PRESET: str = "veryslow"
DEFAULT_X265_PRESET: str = "slow"

# Per-preset accumulator shape used by the --diagnose sweep: preset name (or None for
# the encoder default) -> stats. Values mix ints (byte counts, file counts) and floats
# (seconds), so the value type is broad.
type PresetStats = dict[str | None, dict[str, float]]

# process_file's success/preview result:
# (original_size, new_size, video_duration_seconds_or_None, action, downscaled,
#  grew_larger, retried, interlace_hinted). action is "encoded" or "copied".
# interlace_hinted is True only when the file was encoded with --deinterlace off AND
# its container metadata claimed interlaced video — an advisory flag the run summary
# counts, never a statement that the picture really is interlaced.
type ProcessResult = tuple[int, int, float | None, str, bool, bool, bool, bool]

# One row of the --compare-crf table:
# (crf, output_size_bytes_or_None, elapsed_seconds, error_or_None, skipped).
type CrfRow = tuple[int, int | None, float, str | None, bool]


def compute_timeout_seconds(src_size_bytes: int) -> float:
    """A generous timeout for a single ffprobe metadata read over a file this size, so
    a hung probe on a corrupt or unusual file doesn't stall the batch indefinitely.
    Capped at TIMEOUT_CEILING_SECONDS, since a probe that slow is stuck rather than
    merely working on a big file. Not applied to ffmpeg encode or decode passes, which
    are guarded by the liveness-based stall watchdog instead — see
    run_ffmpeg_with_watchdog and ENCODE_STALL_SECONDS."""
    size_gb = src_size_bytes / (1024 ** 3)
    return min(TIMEOUT_FLOOR_SECONDS + size_gb * TIMEOUT_SECONDS_PER_GB,
               TIMEOUT_CEILING_SECONDS)


def _is_within(path: Path, folder: Path) -> bool:
    """True if path is folder itself or lives anywhere inside it. Both arguments must
    already be resolved (absolute, with symlinks/./.. resolved) for this to be
    meaningful."""
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def relpath_for_matching(src: Path, input_folder: Path) -> str:
    """The path of src relative to input_folder, written with forward slashes, for use
    with --include/--exclude regexes. Forward slashes make patterns portable across
    OSes; matching this relative form (rather than the absolute path) is what lets a
    pattern anchor to the first path component below the input folder."""
    return src.relative_to(input_folder).as_posix()


def filter_files(files: list[Path], input_folder: Path,
                 include_re: re.Pattern | None, exclude_re: re.Pattern | None) -> list[Path]:
    """Apply the compiled --include/--exclude patterns to a list of source paths and
    return the ones to keep. Each is matched (start-anchored, via re.match) against the
    file's path relative to input_folder in forward-slash form. A file is kept when it
    matches include (or include is None) AND does not match exclude; exclude wins any
    tie. include_re/exclude_re are compiled patterns (already case-insensitive) or None."""
    kept: list[Path] = []
    for src in files:
        rel = relpath_for_matching(src, input_folder)
        if include_re is not None and not include_re.match(rel):
            continue
        if exclude_re is not None and exclude_re.match(rel):
            continue
        kept.append(src)
    return kept


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recursively re-encode .mp4/.mkv files to H.265 (hardware via Intel "
                    "Quick Sync by default; use --encoding=software for libx265). Files "
                    "already in H.265 are copied through unchanged, not re-encoded. "
                    "Optional flags add 1080p downscaling (-d), non-English audio "
                    "stripping (-e), and EBU R128 loudness normalization "
                    "(--normalize-audio). See --compare-crf to test-encode files "
                    "at multiple CRF values side by side."
    )
    parser.add_argument("-i", "--input", dest="input_folder", type=Path, default=None,
                         help="Folder to scan recursively for .mp4/.mkv files, or a single "
                              ".mp4/.mkv file to process on its own. If a name is somehow both "
                              "a folder and a file, it's treated as a folder. For a single "
                              "file, the output is written flat at the top of the output "
                              "folder (e.g. -i test.mp4 -o d:\\tmp writes d:\\tmp\\test.mp4). "
                              "Defaults to the current folder, but see -o: at least one of "
                              "-i/-o must be given explicitly, and the two must be different "
                              "folders, neither nested inside the other.")
    parser.add_argument("-o", "--output", dest="output_folder", type=Path, default=None,
                         help="Folder to write converted/copied files to. Defaults to the "
                              "current folder. The output folder must differ from the input "
                              "folder and neither may be nested inside the other (so a run "
                              "never reads its own output or overwrites a source), which means "
                              "at least one of -i/-o must be given explicitly — the two can't "
                              "both fall back to the current folder.")
    parser.add_argument("-q", "--crf", type=int, default=22,
                         help="x265 CRF value (lower = higher quality/larger file). Default: 22")
    parser.add_argument("--source", choices=["all", "dvd", "bd"], default="all",
                         help="Which sources to process: 'dvd' only DVD rips, 'bd' only "
                              "Blu-ray rips, 'all' everything. Judged from each file's "
                              f"frame size: at most {DVD_MAX_WIDTH}x{DVD_MAX_HEIGHT} counts "
                              "as DVD, anything larger as Blu-ray. Lets you run each group "
                              "with its own settings over one mixed folder, e.g. "
                              "'--source dvd --deinterlace auto --encoding software' and "
                              "then '--source bd'. Files outside the chosen group are "
                              "skipped entirely and left out of the totals, ETA and "
                              "--limit. 'dvd' and 'bd' read each file's metadata once at "
                              "startup to sort them. Default: all (nothing is read)")
    parser.add_argument("-t", "--duration", type=float, default=-1,
                         help="Encode only the first N seconds of each file. "
                              "Default: -1 (encode the full file)")
    parser.add_argument("--dry-run", action="store_true",
                         help="List what would be done for each file without actually "
                              "encoding or copying anything.")
    parser.add_argument("--min-size-mb", type=float, default=50,
                         help="Files smaller than this size (in MB) are skipped and just "
                              "copied to the output folder as-is. Default: 50")
    parser.add_argument("-E", "--encoding", choices=["hardware", "software"], default="hardware",
                         help="Encoding mode. 'software' uses libx265 (CPU). 'hardware' uses "
                              "Intel Quick Sync (hevc_qsv). Default: hardware")
    parser.add_argument("-p", "--preset", type=str, default=None,
                         help=f"Override the encoder's speed/quality preset. Valid values "
                              f"depend on --encoding: hardware (hevc_qsv) accepts "
                              f"{', '.join(QSV_PRESETS)}; software (libx265) accepts "
                              f"{', '.join(X265_PRESETS)}. On hardware, preset has a real "
                              f"but much smaller effect on speed than on software — expect "
                              f"a modest slowdown moving toward veryslow, not the large "
                              f"swings seen with libx265. Default: {DEFAULT_QSV_PRESET} for "
                              f"hardware, {DEFAULT_X265_PRESET} for software")
    parser.add_argument("--diagnose", action="store_true",
                         help="Throughput-diagnostic mode. Each source is read once from "
                              "disk and timed to measure delivered read speed; that read "
                              "also warms the OS cache so the encode(s) that follow reflect "
                              "mostly the encoder's own speed rather than disk waiting. "
                              "Behavior depends on whether --preset is given. WITHOUT "
                              "--preset: sweeps every preset for the active encoder "
                              "(hardware or software), encoding each file under all of them "
                              "and printing a comparison table with per-preset encode speed "
                              "(MB/s and GB/day), realtime factor, compression %%, ratio, and "
                              "projected full-job time over the whole input tree — so you "
                              "can pick the fastest preset whose size/quality you like. WITH "
                              "--preset: runs just that preset and reports whether disk I/O "
                              "or encoding is the bottleneck, with a full-job projection. "
                              "Each encode is written to its own preset/CRF-tagged output "
                              "file, so nothing collides. Requires the output folder to "
                              "differ from the input folder. Adds a full extra read per file "
                              "(shared across presets in a sweep), and a software sweep "
                              "includes the very slow presets, so scope it to a handful of "
                              "files with --limit or --include rather than a whole run. "
                              "Default: off")
    parser.add_argument("-n", "--normalize-audio", action="store_true",
                         help="Apply EBU R128 loudness normalization (ffmpeg's loudnorm filter, "
                              "two-pass) to the audio track during encoding. Only affects files "
                              "that are actually re-encoded — files copied as-is (already H.265, "
                              "or below --min-size-mb) are left untouched. Adds an extra "
                              "full-length analysis pass per re-encoded file. Default: off")
    parser.add_argument("--loudnorm-target", type=float, default=-16,
                         help="Integrated loudness target in LUFS, used with --normalize-audio. "
                              "-16 is typical for general/streaming content, -23 is the EBU "
                              "broadcast standard. Default: -16")
    parser.add_argument("-f", "--force", action="store_true",
                         help="Overwrite output files that already exist. Without this flag, "
                              "if a file already exists at the output path, it is silently "
                              "skipped.")
    parser.add_argument("--limit", type=float, default=-1,
                         help="Stop before processing a file that would push the "
                              "cumulative original size past this many GB, so the "
                              "limit acts as a ceiling rather than being overshot. "
                              "Default: -1 (no limit)")
    parser.add_argument("-d", "--downscale", action="store_true",
                         help="Downscale video to fit within 1920x1080 if the source is "
                              "larger, when re-encoding. Without this flag, files are "
                              "encoded at their original resolution regardless of size. "
                              "Default: off")
    parser.add_argument("-e", "--strip-no-english-audio", action="store_true",
                         help="When re-encoding, drop non-English audio tracks if the "
                              "main (first) audio track is tagged English. If the main "
                              "track is tagged as a different language, or has no "
                              "language tag at all, all audio tracks are kept regardless. "
                              "Default: off (all audio tracks are kept)")
    parser.add_argument("-a", "--audio-codec", choices=sorted(AUDIO_CODEC_SETTINGS),
                         default=DEFAULT_AUDIO_CODEC,
                         help="Codec for re-encoded audio (always stereo). 'aac' (160 "
                              "kbps) plays on essentially every TV, streaming box and "
                              "phone, in both MKV and MP4. 'opus' (128 kbps) is more "
                              "efficient but unsupported by many TV players, and Roku "
                              "can't play it in MP4 files. The size difference is ~29 MB "
                              "per two hours of video. Files copied through unchanged "
                              "(already HEVC, or below --min-size-mb) keep their original "
                              f"audio. Default: {DEFAULT_AUDIO_CODEC}")
    parser.add_argument("-D", "--deinterlace", choices=["off", "auto", "deinterlace", "detelecine"],
                         default="off",
                         help="How to handle interlaced sources (mainly DVD rips; Blu-ray "
                              "is almost always progressive). HEVC has no interlaced "
                              "coding mode, so interlaced video encoded as-is gets its "
                              "comb artifacts baked in permanently and costs extra "
                              "bitrate to store them. 'auto' decodes a short sample of "
                              "each file and picks per file: film-sourced NTSC content "
                              "carried as 3:2 pulldown is inverse-telecined back to true "
                              "23.976p (better quality AND ~20%% fewer frames), genuinely "
                              "interlaced video is deinterlaced with bwdif, and "
                              "progressive content is left untouched. 'deinterlace' and "
                              "'detelecine' force one filter on every file without "
                              "detecting anything, for sources you already know the "
                              "answer for. Detection adds a second or two per file and "
                              "only runs on files being re-encoded, never on copies. "
                              "Default: off (video passed through exactly as before)")
    parser.add_argument("-I", "--include", type=str, default=None, metavar="REGEX",
                         help="Only process files whose path, taken relative to the "
                              "input folder and written with forward slashes, matches "
                              "this regex at its start (like re.match, not a full "
                              "match). E.g. with -i d:/media, --include=ABC processes "
                              "d:/media/ABCdef/title.mkv but not d:/media/zABC/title.mkv. "
                              "Case-insensitive. Files that don't match are skipped "
                              "entirely (never copied or encoded). Default: "
                              "process everything")
    parser.add_argument("-X", "--exclude", type=str, default=None, metavar="REGEX",
                         help="Skip files whose path, taken relative to the input "
                              "folder and written with forward slashes, matches this "
                              "regex at its start (like re.match). Same anchoring and "
                              "case-insensitivity as --include. When a file matches "
                              "both --include and --exclude, --exclude wins and the "
                              "file is skipped. Default: exclude nothing")
    parser.add_argument("--compare-crf", type=str, default=None, metavar="CRF1,CRF2,...",
                         help="Comparison mode: test-encode every file at each given CRF "
                              "(e.g. 18,22,28,35), printing a size/time table per file plus "
                              "an aggregate table across files. Uses --duration for a quick "
                              "clip test if set, otherwise encodes full files. Requires "
                              "-o/--output different from -i/--input. Writes "
                              "<name>_crf<value>_<hardware|software>_<preset><ext> plus an unmodified "
                              "<name>_original<ext> for side-by-side comparison.")
    return parser


def parse_args() -> argparse.Namespace:
    return build_parser().parse_args()


class ColorConsoleFormatter(logging.Formatter):
    """Colors WARNING messages yellow and ERROR/CRITICAL messages red when printed
    to the console. COLOR_WARNING/COLOR_ERROR/COLOR_RESET are empty strings when
    stderr isn't a real terminal, so redirected/piped output stays plain text."""
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if record.levelno >= logging.ERROR:
            return f"{COLOR_ERROR}{message}{COLOR_RESET}"
        if record.levelno >= logging.WARNING:
            return f"{COLOR_WARNING}{message}{COLOR_RESET}"
        return message


def setup_logging(output_folder: Path, name_suffix: str = "") -> Path:
    """Configures file + console logging and returns the log file's path. name_suffix,
    when given, is appended to the log filename (e.g. "_software" ->
    conversion_log_software.txt) so --compare-crf runs of the same folder with
    different encoders don't append to a single shared log."""
    output_folder.mkdir(parents=True, exist_ok=True)
    log_path = output_folder / f"conversion_log{name_suffix}.txt"
    log_format = "%(asctime)s [%(levelname)s] %(message)s"

    # Full detail (INFO and up) goes to the log file, plain text. Opened in append
    # mode (FileHandler's default, stated explicitly here so it can't be changed by
    # accident) so repeated runs against the same output folder — e.g. --compare-crf
    # re-runs adding new CRF values — accumulate in one log rather than each run
    # wiping the previous run's record.
    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(log_format))

    # Only WARNING and above are echoed to the console, in color, so they stand
    # out from the plain per-file progress line without cluttering it with the
    # full per-file INFO detail (which stays log-file only).
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(ColorConsoleFormatter(log_format))

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
    return log_path


# Codecs that are almost always an embedded cover-art/thumbnail image rather than
# real video, used as a fallback signal when a container doesn't set the
# attached_pic disposition flag correctly.
COVER_ART_CODECS = {"mjpeg", "png", "bmp", "gif"}


def _parse_frame_rate(rate_str: str | None) -> float | None:
    """Parse ffprobe's r_frame_rate, which is a rational string like '30000/1001',
    into a float. Returns None for a missing, malformed or zero-denominator value
    (ffprobe reports '0/0' for streams where it can't determine a rate)."""
    if not rate_str:
        return None
    try:
        if "/" in rate_str:
            num_str, _, den_str = rate_str.partition("/")
            num, den = float(num_str), float(den_str)
            return num / den if den else None
        return float(rate_str)
    except ValueError:
        return None


def probe_media(path: Path, timeout_seconds: float) -> dict[str, Any]:
    """Probe a media file with a single ffprobe call, returning:
      {
        "video": {"codec_name": str, "width": int, "height": int, "duration": float|None,
                   "stream_index": int,   # index i in ffmpeg's 0:v:i, for explicit mapping
                   "frame_rate": float|None, "field_order": str|None},
        "audio_languages": [lang_or_None, ...],   # index i == ffmpeg's 0:a:i
        "subtitle_tracks": [(lang_or_None, codec_name), ...],  # index i == 0:s:i
      }
    The video stream chosen is the first one that isn't embedded cover art (an
    attached_pic, or an image-y codec like mjpeg/png/bmp/gif), so a thumbnail
    that precedes the real video stream isn't mistaken for it.
    Raises ConversionError if ffprobe fails or no real video stream is found, or
    ConversionTimeoutError if it doesn't finish within timeout_seconds."""
    cmd: list[str] = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "stream=codec_name,codec_type,width,height,disposition,r_frame_rate,field_order:"
        "stream_tags=language:format=duration",
        "-of", "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True,
                                 timeout=timeout_seconds)
        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise ConversionTimeoutError(path, f"ffprobe timed out after {timeout_seconds:.0f}s")
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError) as e:
        raise ConversionError(path, f"ffprobe failed: {e}")

    video_candidates: list[dict[str, Any]] = []  # every non-cover-art video stream seen, in order, with its 0:v:i index
    fallback_video_info: dict[str, Any] | None = None  # first video stream at all, in case every one looks like cover art
    audio_languages: list[str | None] = []
    subtitle_tracks: list[tuple[str | None, str]] = []
    video_stream_count = 0

    for s in data.get("streams", []):
        codec_type = s.get("codec_type")
        lang = s.get("tags", {}).get("language")
        lang = lang.lower() if lang else None

        if codec_type == "video":
            stream_index = video_stream_count
            video_stream_count += 1
            codec_name = s.get("codec_name", "")
            is_attached_pic = bool(s.get("disposition", {}).get("attached_pic"))
            is_cover_art = is_attached_pic or codec_name in COVER_ART_CODECS

            info = {
                "codec_name": codec_name,
                "width": s.get("width", 0),
                "height": s.get("height", 0),
                "stream_index": stream_index,
                # Container-declared frame rate and field order. Both are hints only:
                # field_order is frequently wrong or absent on DVD rips (and stays
                # stale on a re-muxed file), so detect_field_mode() decides from the
                # actual picture content instead. frame_rate matters because it's what
                # separates an NTSC 29.97 source (where 3:2 telecine is possible) from
                # a PAL 25 source (where it isn't).
                "frame_rate": _parse_frame_rate(s.get("r_frame_rate")),
                "field_order": s.get("field_order"),
            }
            if fallback_video_info is None:
                fallback_video_info = info
            if not is_cover_art:
                video_candidates.append(info)
        elif codec_type == "audio":
            audio_languages.append(lang)
        elif codec_type == "subtitle":
            subtitle_tracks.append((lang, s.get("codec_name", "unknown")))

    if video_candidates:
        video_info = video_candidates[0]
    elif fallback_video_info is not None:
        # Every video stream looked like cover art (e.g. a file with only an
        # embedded thumbnail and no real video track). Fall back to the first
        # one rather than failing outright, since that matches prior behavior.
        video_info = fallback_video_info
    else:
        raise ConversionError(path, "no video stream found")

    duration_str = data.get("format", {}).get("duration")
    try:
        video_info["duration"] = float(duration_str) if duration_str is not None else None
    except ValueError:
        video_info["duration"] = None

    return {
        "video": video_info,
        "audio_languages": audio_languages,
        "subtitle_tracks": subtitle_tracks,
    }


def probe_duration(path: Path, timeout_seconds: float) -> float | None:
    """Return the duration (seconds) of a media file via a lightweight ffprobe call, or
    None if duration could not be determined. Raises ConversionError if ffprobe itself
    fails to read the file (a strong signal of a corrupt/incomplete output), or
    ConversionTimeoutError if it doesn't finish within timeout_seconds."""
    cmd: list[str] = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True,
                                 timeout=timeout_seconds)
        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise ConversionTimeoutError(path, f"ffprobe timed out after {timeout_seconds:.0f}s")
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError) as e:
        raise ConversionError(path, f"ffprobe (post-encode validation) failed: {e}")

    duration_str = data.get("format", {}).get("duration")
    try:
        return float(duration_str) if duration_str is not None else None
    except ValueError:
        return None


def _process_cpu_seconds(proc: "subprocess.Popen[str]") -> float | None:
    """Total CPU time (user + kernel, in seconds) consumed so far by a running child
    process, or None if it can't be determined on this platform/process. Used by
    run_ffmpeg_with_watchdog as a liveness signal that stays valid during the silent
    tail of an encode, when no -progress heartbeats are emitted but the encoder is
    still working hard.

    Windows reads it via GetProcessTimes on the handle subprocess already holds; Linux
    reads utime+stime from /proc. Never raises: if sampling fails for any reason the
    caller simply falls back to heartbeats alone (see run_ffmpeg_with_watchdog)."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            handle = getattr(proc, "_handle", None)
            if handle is None:
                return None
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            ok = ctypes.windll.kernel32.GetProcessTimes(  # type: ignore[attr-defined]
                wintypes.HANDLE(int(handle)),
                ctypes.byref(creation), ctypes.byref(exit_time),
                ctypes.byref(kernel), ctypes.byref(user))
            if not ok:
                return None
            # FILETIME is a split 64-bit count of 100-nanosecond intervals.
            def _ft(ft: "wintypes.FILETIME") -> int:
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
            return (_ft(kernel) + _ft(user)) / 10_000_000.0
        with open(f"/proc/{proc.pid}/stat", "rb") as fh:
            # The comm field can contain spaces/parens, so split after the closing
            # paren; utime/stime are then fields 14 and 15 (1-based) of the original.
            fields = fh.read().rpartition(b")")[2].split()
        utime, stime = int(fields[11]), int(fields[12])
        return (utime + stime) / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def add_progress_flags(cmd: list[str]) -> list[str]:
    """Return cmd with the global options the stall watchdog needs, inserted directly
    after the ffmpeg executable so they're parsed as global (not per-input) options.
    Call this before logging the command, so what gets logged is exactly what runs.

    -progress pipe:1 emits a machine-readable heartbeat block to stdout roughly once a
    second; -nostats drops the human progress line we'd otherwise have to filter out;
    -nostdin stops ffmpeg consuming the console's stdin, which matters now that we run
    it via Popen rather than a fully-captured subprocess.run. stdout is free for this
    in every command the script builds: real output always goes to a file, and the
    loudness pass writes to the null muxer."""
    return [cmd[0], "-nostdin", "-progress", "pipe:1", "-nostats"] + list(cmd[1:])


class FFmpegRunResult:
    """Outcome of one watchdog-supervised ffmpeg run."""
    def __init__(self, returncode: int, stderr: str, stalled: bool, elapsed: float,
                 silent_for: float) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stalled = stalled          # True if the watchdog killed it as hung
        self.elapsed = elapsed
        self.silent_for = silent_for    # seconds of combined silence that triggered the kill


def run_ffmpeg_with_watchdog(cmd: list[str],
                              stall_seconds: float = ENCODE_STALL_SECONDS
                              ) -> FFmpegRunResult:
    """Run an ffmpeg command to completion, killing it only if it stops showing any
    sign of life for stall_seconds. cmd must already carry the progress flags (see
    add_progress_flags).

    Liveness is the OR of two signals, and the process is killed only when both have
    been flat for the whole window:
      * a -progress heartbeat arriving on stdout, and
      * consumed CPU time continuing to climb.
    Elapsed wall time is deliberately NOT a factor, so a legitimately slow encode can
    run as long as it needs. The CPU signal covers the silent tail where the encoder
    drains its lookahead and the muxer writes its index; the heartbeat signal covers
    any case where CPU sampling is unavailable. If CPU time can't be sampled at all on
    this platform, the watchdog degrades to heartbeats alone, which is still correct
    for a wedged process — just likelier to misjudge a very long flush, which is why
    stall_seconds is set in minutes rather than seconds.

    Both pipes are drained on background threads: stderr in particular must be read
    continuously, or a chatty ffmpeg would fill the pipe buffer and deadlock waiting
    for us while we wait for it. Returns a FFmpegRunResult; never raises on a failed
    or killed encode (the caller decides what a non-zero return code means)."""
    import queue
    import threading

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, errors="replace")
    heartbeats: queue.Queue[float | None] = queue.Queue()
    stderr_chunks: list[str] = []

    def pump_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            # Each progress block ends with a progress=continue/end line; counting only
            # those keeps one heartbeat per block instead of one per key.
            if line.startswith("progress="):
                heartbeats.put(time.monotonic())
        heartbeats.put(None)  # stdout closed: ffmpeg is on its way out

    def pump_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)

    t_out = threading.Thread(target=pump_stdout, daemon=True)
    t_err = threading.Thread(target=pump_stderr, daemon=True)
    t_out.start()
    t_err.start()

    start = time.monotonic()
    last_life = start                       # last moment either signal moved
    last_cpu = _process_cpu_seconds(proc)
    stalled = False
    stdout_closed = False

    # If anything interrupts the wait — Ctrl-C above all — the ffmpeg child must die
    # with us. subprocess.run does this automatically, but a raw Popen doesn't, and an
    # orphaned ffmpeg would keep running after the script exits, still writing (and
    # holding open) a partial output file.
    try:
        while True:
            if stdout_closed:
                # ffmpeg has closed stdout; it's finishing up. Wait for exit, still
                # watching CPU so a wedge during final teardown is caught too.
                if proc.poll() is not None:
                    break
            now = time.monotonic()
            if now - last_life >= stall_seconds:
                stalled = True
                proc.kill()
                break

            try:
                beat = heartbeats.get(timeout=ENCODE_STALL_POLL_SECONDS)
                if beat is None:
                    stdout_closed = True
                else:
                    last_life = beat
                    last_cpu = _process_cpu_seconds(proc)
                    continue
            except queue.Empty:
                pass

            # No heartbeat this interval — fall back to the CPU signal.
            cpu_now = _process_cpu_seconds(proc)
            if cpu_now is not None and last_cpu is not None and \
                    cpu_now - last_cpu >= ENCODE_STALL_MIN_CPU_DELTA:
                last_life = time.monotonic()
            if cpu_now is not None:
                last_cpu = cpu_now
    except BaseException:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass
        raise

    returncode = proc.wait()
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    elapsed = time.monotonic() - start
    silent_for = time.monotonic() - last_life if stalled else 0.0
    return FFmpegRunResult(returncode, "".join(stderr_chunks), stalled, elapsed,
                            silent_for)


def _run_idet(src: Path, start_seconds: float, extra_filters: str | None
               ) -> dict[str, int] | None:
    """Decode a sample of src through ffmpeg's idet filter and return its frame counts
    as {'interlaced', 'progressive', 'undetermined', 'repeated', 'total'}, or None if
    the sample couldn't be analyzed. extra_filters, when given, is applied BEFORE idet,
    which is how the telecine test re-measures a field-matched version of the sample.

    'repeated' counts frames carrying a repeated field, which is the fingerprint of 3:2
    pulldown: telecine duplicates one field every five frames, and nothing else does.

    Reads idet's "Multi frame detection" line rather than "Single frame detection":
    the multi-frame figure considers neighbouring frames and is markedly more reliable
    on compressed sources, where a single frame's comb can be ambiguous."""
    filters = f"{extra_filters},idet" if extra_filters else "idet"
    cmd: list[str] = ["ffmpeg", "-hide_banner", "-nostdin"]
    if start_seconds > 0:
        # Before -i, so this is a fast keyframe seek rather than a decode-and-discard.
        cmd += ["-ss", f"{start_seconds:.3f}"]
    cmd += ["-i", str(src), "-map", "0:v:0", "-vf", filters,
            "-frames:v", str(IDET_SAMPLE_FRAMES), "-an", "-sn", "-f", "null", "-"]

    result = run_ffmpeg_with_watchdog(add_progress_flags(cmd))
    if result.stalled or result.returncode != 0:
        return None

    # idet prints its totals at end of stream, e.g.
    #   [Parsed_idet_0 @ ...] Multi frame detection: TFF: 202 BFF: 0 Progressive: 0 Undetermined: 0
    # ffmpeg emits MORE THAN ONE of these blocks: an all-zero one from a filter-graph
    # instance that processed no frames, then the real totals from the instance that
    # did the work. Take the last block, never the first — reading the first is a
    # silent failure that makes every file look unclassifiable.
    matches = re.findall(
        r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*"
        r"Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)", result.stderr)
    if not matches:
        return None
    tff, bff, progressive, undetermined = (int(g) for g in matches[-1])

    repeated = 0
    rep_matches = re.findall(
        r"Repeated Fields:\s*Neither:\s*(\d+)\s*Top:\s*(\d+)\s*Bottom:\s*(\d+)",
        result.stderr)
    if rep_matches:
        _, top, bottom = (int(g) for g in rep_matches[-1])
        repeated = top + bottom

    total = tff + bff + progressive + undetermined
    return {"interlaced": tff + bff, "progressive": progressive,
            "undetermined": undetermined, "repeated": repeated, "total": total}


def detect_field_mode(src: Path, video_info: dict[str, Any]) -> tuple[str, str]:
    """Classify src's scan type by inspecting the picture itself, returning
    (mode, reason) where mode is one of:
      'progressive' - leave the video alone
      'telecine'    - 3:2 pulldown; inverse-telecine it (DETELECINE_FILTER)
      'interlaced'  - genuine interlace; deinterlace it (DEINTERLACE_FILTER)
      'unknown'     - detection failed; caller should leave the video alone
    reason is a short human-readable explanation for the log, so a questionable call
    can be audited after the fact rather than silently trusted.

    Stage 1 asks whether there is comb at all. Note that ratios are taken over EVERY
    sampled frame, undetermined ones included: clean progressive footage very often
    comes back entirely 'undetermined' rather than 'progressive' (idet only reports
    'progressive' when it is positively confident), so dividing by the classified
    frames alone turns a handful of stray detections in a pristine file into a 100%
    interlaced verdict.

    Stage 2 separates telecine from true interlace, for NTSC-rate sources only, using
    two independent signals and accepting either:
      * repeated fields - 3:2 pulldown duplicates a field every five frames, giving a
        repeat rate near 20%+; genuine interlace repeats none. This is the strongest
        signal and survives compression well.
      * fieldmatch recovery - re-measuring the sample through fieldmatch. If the comb
        largely disappears, the fields could be re-paired into whole frames, which is
        only true of a pulldown cadence. Verified on clean telecine, where fieldmatch
        takes the sample from ~100% combed to 0%; it degrades on very high-entropy
        compressed sources where lossy coding has broken field correspondence, which
        is why the repeated-field signal stands alongside it rather than behind it.

    Detection never raises: any failure returns 'unknown', because a file whose scan
    type can't be determined should pass through untouched rather than be guessed at."""
    duration = video_info.get("duration")
    start = 0.0
    if isinstance(duration, (int, float)) and duration and duration > 0:
        start = max(0.0, float(duration) * IDET_SAMPLE_POSITION)

    counts = _run_idet(src, start, None)
    if counts is None and start > 0:
        # The seek may have landed past the last keyframe on a short or oddly indexed
        # file; retry from the beginning before giving up.
        counts = _run_idet(src, 0.0, None)
    if counts is None:
        return ("unknown", "idet analysis failed")

    total = counts["total"]
    if total < IDET_MIN_SAMPLE_FRAMES:
        return ("unknown", f"only {total} frame(s) sampled, too few to classify")

    interlaced_ratio = counts["interlaced"] / total
    if interlaced_ratio < IDET_INTERLACED_THRESHOLD:
        return ("progressive",
                f"{interlaced_ratio:.0%} of {total} sampled frames show comb, below the "
                f"{IDET_INTERLACED_THRESHOLD:.0%} threshold")

    frame_rate = video_info.get("frame_rate")
    if isinstance(frame_rate, (int, float)) and any(
            abs(frame_rate - rate) <= PROGRESSIVE_FRAME_RATE_TOLERANCE
            for rate in PROGRESSIVE_FRAME_RATES):
        # See PROGRESSIVE_FRAME_RATES: film rate rules out interlaced video, so this
        # is idet reacting to sharp detail rather than real comb.
        return ("progressive",
                f"{interlaced_ratio:.0%} of {total} sampled frames show comb, but the "
                f"source is {frame_rate:.3f}fps film rate, which no interlaced format "
                f"uses - treating as progressive")

    is_ntsc_rate = isinstance(frame_rate, (int, float)) and any(
        abs(frame_rate - rate) <= NTSC_FRAME_RATE_TOLERANCE for rate in NTSC_FRAME_RATES)
    if not is_ntsc_rate:
        rate_str = f"{frame_rate:.3f}fps" if isinstance(frame_rate, (int, float)) else "unknown rate"
        return ("interlaced",
                f"{interlaced_ratio:.0%} of {total} sampled frames show comb at "
                f"{rate_str}; 3:2 pulldown only occurs at NTSC rates, so treating as "
                f"true interlace")

    repeat_ratio = counts["repeated"] / total
    if repeat_ratio >= IDET_REPEAT_FIELD_THRESHOLD:
        return ("telecine",
                f"{interlaced_ratio:.0%} of {total} sampled frames show comb and "
                f"{repeat_ratio:.0%} carry a repeated field - 3:2 pulldown")

    matched = _run_idet(src, start, "fieldmatch=order=auto:combmatch=full")
    if matched is None or matched["total"] < IDET_MIN_SAMPLE_FRAMES:
        return ("interlaced",
                f"{interlaced_ratio:.0%} of {total} sampled frames show comb, only "
                f"{repeat_ratio:.0%} repeated fields, and the fieldmatch test was "
                f"inconclusive - treating as true interlace")

    matched_ratio = matched["interlaced"] / matched["total"]
    if matched_ratio <= interlaced_ratio * (1 - IDET_FIELDMATCH_RECOVERY):
        return ("telecine",
                f"{interlaced_ratio:.0%} of {total} sampled frames show comb, but field "
                f"matching brings that down to {matched_ratio:.0%} - 3:2 pulldown")
    return ("interlaced",
            f"{interlaced_ratio:.0%} of {total} sampled frames show comb, only "
            f"{repeat_ratio:.0%} repeated fields, and field matching only reaches "
            f"{matched_ratio:.0%} - genuine interlace")


def container_interlace_hint(video_info: dict[str, Any]) -> str | None:
    """A zero-cost advisory check for interlacing, returning a short reason string when
    the container's own metadata says the video is interlaced, or None otherwise.

    Unlike detect_field_mode this decodes NOTHING — it only reads the field_order and
    frame rate that probe_media already collected, so it's free to run on every file.
    That also makes it far weaker evidence: field_order is frequently absent, and on a
    re-muxed file it can be stale (a previous conversion may have left an interlaced
    tag on video that is now progressive, or vice versa). So this is only ever used to
    print a 'worth checking' hint when --deinterlace is off; it never selects a filter
    or changes the encode. A missing tag produces no hint rather than a guess, to keep
    the signal-to-noise ratio worth reading in a long batch log."""
    field_order = video_info.get("field_order")
    if not isinstance(field_order, str):
        return None
    if field_order.lower() in ("progressive", "unknown"):
        return None

    frame_rate = video_info.get("frame_rate")
    rate_part = (f" at {frame_rate:.3f}fps" if isinstance(frame_rate, (int, float))
                 else "")
    return f"container reports field order '{field_order}'{rate_part}"


def resolve_deinterlace_filter(src: Path, video_info: dict[str, Any], mode: str
                                ) -> tuple[str | None, str]:
    """Map the --deinterlace setting to an actual filter string for this file,
    returning (filter_or_None, log_note). 'auto' runs detection; 'deinterlace' and
    'detelecine' force the corresponding filter without detection (an escape hatch for
    files the detector calls wrong); 'off' disables the feature entirely."""
    if mode == "off":
        return (None, "off")
    if mode == "deinterlace":
        return (DEINTERLACE_FILTER, "forced deinterlace (detection skipped)")
    if mode == "detelecine":
        return (DETELECINE_FILTER, "forced inverse telecine (detection skipped)")

    detected, reason = detect_field_mode(src, video_info)
    if detected == "telecine":
        return (DETELECINE_FILTER, f"auto -> inverse telecine ({reason})")
    if detected == "interlaced":
        return (DEINTERLACE_FILTER, f"auto -> deinterlace ({reason})")
    if detected == "unknown":
        return (None, f"auto -> leaving video untouched ({reason})")
    return (None, f"auto -> progressive, no filtering ({reason})")


def measure_subtitle_bytes(path: Path) -> tuple[int, int] | None:
    """Total size in bytes of all subtitle-stream packets in path, with the number of
    subtitle tracks, or None if it couldn't be measured. Informational only — nothing
    in the conversion depends on it.

    ffprobe is asked for packet sizes on subtitle streams only. It still walks the
    whole container to find them, so cost tracks file size rather than subtitle size;
    measured here at roughly 1.4 GB/s against warm cache, meaning it's CPU-trivial and
    in practice bounded by how fast the output file can be re-read. That's acceptable
    because it runs on the freshly written output, much of which the OS still has
    cached, and because it's a small fraction of the encode that just produced it.

    Never raises: a probe failure returns None and the caller simply reports nothing,
    since a missing statistic must not disturb a successful conversion."""
    cmd: list[str] = ["ffprobe", "-v", "error", "-select_streams", "s",
                      "-show_entries", "packet=size", "-of", "compact=p=0:nk=1",
                      str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=SUBTITLE_MEASURE_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None

    total = 0
    for line in result.stdout.split():
        try:
            total += int(line)
        except ValueError:
            continue  # ffprobe emits "N/A" for packets with no known size

    count_cmd: list[str] = ["ffprobe", "-v", "error", "-select_streams", "s",
                            "-show_entries", "stream=index", "-of", "csv=p=0",
                            str(path)]
    tracks = 0
    try:
        count_result = subprocess.run(count_cmd, capture_output=True, text=True,
                                       timeout=SUBTITLE_MEASURE_TIMEOUT_SECONDS)
        if count_result.returncode == 0:
            tracks = len([ln for ln in count_result.stdout.splitlines() if ln.strip()])
    except (subprocess.TimeoutExpired, OSError):
        pass
    return (total, tracks)


def measure_loudness(src: Path, duration: float, loudnorm_target: float,
                      audio_stream_index: int) -> dict[str, Any]:
    """Runs loudnorm's analysis pass (decode + filter, no output file written) against
    the given audio stream to measure its actual loudness stats, for feeding into a
    second, exact pass of EBU R128 normalization. Returns the parsed stats dict
    (keys include input_i, input_tp, input_lra, input_thresh, target_offset).
    Raises ConversionError if ffmpeg fails, stalls, or the stats block can't be
    found/parsed, so callers can fall back to one-pass normalization for this file.
    No wall-clock cap is applied — a full decode pass on a large file can legitimately
    run a long time — but the pass is supervised by the liveness-based stall watchdog,
    so a wedged decode is caught without false-positiving on slow-but-healthy work."""
    cmd: list[str] = ["ffmpeg", "-i", str(src)]
    if duration != -1:
        cmd += ["-t", str(duration)]
    cmd += [
        "-map", f"0:a:{audio_stream_index}",
        "-af", f"loudnorm=I={loudnorm_target}:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-",
    ]
    cmd = add_progress_flags(cmd)
    log_ffmpeg_command(cmd)
    result = run_ffmpeg_with_watchdog(cmd)
    if result.stalled:
        raise ConversionError(src, f"loudness measurement pass stalled: no progress and "
                                    f"no CPU activity for "
                                    f"{human_duration(result.silent_for, include_seconds=True)}; "
                                    f"killed after "
                                    f"{human_duration(result.elapsed, include_seconds=True)}")
    if result.returncode != 0:
        raise ConversionError(src, f"loudness measurement pass failed: {result.stderr[-1000:]}")

    # loudnorm prints its stats as a single JSON object to stderr, surrounded by
    # ordinary log lines. There's no start/end marker, so pull out the last
    # brace-delimited block (the filter has no nested braces of its own).
    matches = re.findall(r"\{[^{}]+\}", result.stderr)
    if not matches:
        raise ConversionError(src, "loudness measurement pass produced no stats output")
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError as e:
        raise ConversionError(src, f"could not parse loudness measurement stats: {e}")


def human_size(num_bytes: float) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def human_duration(seconds: float, include_seconds: bool = False) -> str:
    """Formats a duration for display. By default rounds to the nearest minute
    (used for video content duration, where second-level precision isn't
    meaningful). With include_seconds=True, keeps seconds precision instead —
    used for the script's own wall-clock runtime, where seconds matter (e.g.
    comparing hardware vs. software encoding speed on a short test run)."""
    if include_seconds:
        total_seconds = int(round(seconds))
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, secs = divmod(remainder, 60)
        if days:
            return f"{days}d {hours}h {minutes}m {secs}s"
        if hours:
            return f"{hours}h {minutes}m {secs}s"
        if minutes:
            return f"{minutes}m {secs}s"
        return f"{secs}s"

    total_minutes = int(round(seconds / 60))
    days, remainder = divmod(total_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def measure_read_speed(path: Path, chunk_size: int = 8 * 1024 * 1024) -> tuple[float, int]:
    """Sequentially read the whole file from disk, timed, and discard the bytes.
    Returns (elapsed_seconds, bytes_read). Two purposes: (1) measure the disk's
    delivered read throughput for this file, and (2) warm the OS page cache so an
    encode run immediately afterward serves its reads from RAM — isolating the
    encoder's own speed from disk-wait. Note: for a file larger than free RAM the
    cache can't hold all of it, so the following encode still incurs some real I/O;
    the comparison stays self-consistent (a file that won't cache is exactly one that
    stays I/O bound in production), just less cleanly separated."""
    start = time.monotonic()
    total: int = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
    return (time.monotonic() - start, total)


def mbps_to_gb_per_day(mbps: float) -> float:
    """Convert a sustained MB/s throughput into GB/day, the unit the whole 60TB job is
    naturally reasoned about in (e.g. 'the disk delivers ~X GB/day')."""
    return mbps * 86400 / 1024


def format_throughput(bytes_moved: float, seconds: float) -> str:
    """A 'X.X MB/s (Y GB/day)' string for a byte count moved over a wall-clock span."""
    if seconds <= 0:
        return "n/a"
    mbps = bytes_moved / (1024 * 1024) / seconds
    return f"{mbps:.1f} MB/s ({mbps_to_gb_per_day(mbps):.0f} GB/day)"


def build_preset_comparison_table(preset_stats: PresetStats,
                                   presets_order: tuple[str | None, ...],
                                   total_size_bytes: int) -> list[str]:
    """Render a fixed-width table comparing each tested preset, one row per preset, in
    the given order. preset_stats maps preset name -> dict with keys enc_seconds,
    enc_bytes (source bytes encoded), new_bytes (output bytes), video_seconds, files.
    total_size_bytes is the whole input tree, used to project a full-job time per
    preset from that preset's measured encode speed. Returns a list of text lines.
    Presets with no successfully-encoded files are shown with '-' placeholders so the
    row still appears (useful to see which presets errored out)."""
    header: str = (f"{'preset':<10} {'enc MB/s':>9} {'GB/day':>8} {'realtime':>9} "
                   f"{'smaller':>8} {'ratio':>6} {'proj. full job':>16}")
    sep: str = "-" * len(header)
    lines: list[str] = [header, sep]
    for preset in presets_order:
        s = preset_stats.get(preset)
        if not s or s["files"] == 0 or s["enc_seconds"] <= 0:
            lines.append(f"{preset:<10} {'-':>9} {'-':>8} {'-':>9} {'-':>8} "
                         f"{'-':>6} {'-':>16}")
            continue
        enc_mbps = s["enc_bytes"] / (1024 * 1024) / s["enc_seconds"]
        gb_day = mbps_to_gb_per_day(enc_mbps)
        realtime = (s["video_seconds"] / s["enc_seconds"]) if s["video_seconds"] > 0 else 0
        pct_smaller = (1 - s["new_bytes"] / s["enc_bytes"]) * 100 if s["enc_bytes"] > 0 else 0
        ratio = s["enc_bytes"] / s["new_bytes"] if s["new_bytes"] > 0 else 0
        if enc_mbps > 0 and total_size_bytes > 0:
            proj_seconds = total_size_bytes / (enc_mbps * 1024 * 1024)
            proj = human_duration(proj_seconds)
        else:
            proj = "-"
        rt_str = f"{realtime:.1f}x" if realtime else "-"
        lines.append(f"{preset:<10} {enc_mbps:>9.1f} {gb_day:>8.0f} {rt_str:>9} "
                     f"{pct_smaller:>7.0f}% {ratio:>5.2f}x {proj:>16}")
    return lines


# A source counts as DVD-sourced if its frame fits within standard-definition limits:
# 720x480 (NTSC) and 720x576 (PAL), with headroom for rips stored at square-pixel sizes
# such as 854x480 or 1024x576. Anything larger is treated as Blu-ray. Both limits must
# hold: a scope film cropped to 1280x536 is under 576 lines tall but is unmistakably HD.
DVD_MAX_WIDTH: int = 1024
DVD_MAX_HEIGHT: int = 576


# Per-run cache of classify_source results, so no file is probed twice for the same
# answer.
_SOURCE_CACHE: dict[Path, tuple[str, int, int] | None] = {}


def classify_source(src: Path) -> tuple[str, int, int] | None:
    """Classify src as 'dvd' or 'bd' from its frame size, returning (type, width,
    height), or None if the file can't be probed. A file counts as DVD when its frame
    fits within DVD_MAX_WIDTH x DVD_MAX_HEIGHT; anything larger is Blu-ray. Resolution
    is the only reliable signal here: a ripped file carries no marker of the disc it
    came from, and codec doesn't settle it (MPEG-2 appears on both). Cached per file."""
    if src in _SOURCE_CACHE:
        return _SOURCE_CACHE[src]
    result: tuple[str, int, int] | None
    try:
        video = probe_media(src, compute_timeout_seconds(src.stat().st_size))["video"]
        width, height = video.get("width") or 0, video.get("height") or 0
        if not (width and height):
            result = None
        elif width <= DVD_MAX_WIDTH and height <= DVD_MAX_HEIGHT:
            result = ("dvd", width, height)
        else:
            result = ("bd", width, height)
    except (ConversionError, OSError):
        result = None
    _SOURCE_CACHE[src] = result
    return result


SOURCE_LABELS: dict[str, str] = {"dvd": "DVD", "bd": "Blu-ray"}


def diagnose_encode_one(src: Path, dst: Path, preset: str | None, read_seconds: float,
                         read_bytes: int, args: argparse.Namespace) -> dict[str, float] | None:
    """Encode a single file under a single preset for diagnostics, with the OS cache
    assumed already warmed by a prior read of src. Returns a dict of this encode's
    stats (enc_seconds, orig_bytes, new_bytes, video_seconds) on success, or None if
    the file was copied (not encoded), failed, or errored. Also logs a per-file
    [DIAGNOSE] line comparing disk read speed against this preset's encode speed. The
    caller handles accumulation and the read measurement; this isolates the
    encode-and-measure of one (file, preset) pair."""
    call_start = time.monotonic()
    try:
        result: ProcessResult | None = process_file(
            src, dst, args.crf, args.duration, args.min_size_mb,
            args.dry_run, args.encoding,
            args.normalize_audio, args.loudnorm_target, args.downscale,
            args.strip_no_english_audio, preset,
            deinterlace=args.deinterlace, audio_codec=args.audio_codec)
    except ConversionError as e:
        logging.error(f"CONVERSION FAILED (preset {preset}): {e.file.resolve()}\n{e.reason}")
        return None
    call_elapsed = time.monotonic() - call_start

    if result is None:
        return None
    (orig_size, new_size, video_duration, action, downscaled, grew_larger,
     retried, interlace_hinted) = result
    if action != "encoded" or retried:
        # Copied (already HEVC / below min size) or retried (timing polluted by the
        # failed attempt) — not a clean encode-speed sample.
        return None

    label: str = preset if preset is not None else "default"
    read_mbps = read_bytes / (1024 * 1024) / read_seconds if read_seconds > 0 else 0
    enc_mbps = orig_size / (1024 * 1024) / call_elapsed if call_elapsed > 0 else 0
    pct_smaller = (1 - new_size / orig_size) * 100 if orig_size > 0 else 0
    size_part = f"{human_size(orig_size)}->{human_size(new_size)}, {pct_smaller:.0f}% smaller"
    rt_part = (f", {video_duration / call_elapsed:.1f}x realtime"
               if video_duration and call_elapsed > 0 else "")
    diag_line = (f"[DIAGNOSE] {src.name} [{label}]: "
                 f"read {format_throughput(read_bytes, read_seconds)} vs "
                 f"encode {format_throughput(orig_size, call_elapsed)}{rt_part} | {size_part}")
    logging.info(diag_line)
    print(diag_line)

    return {"enc_seconds": call_elapsed, "orig_bytes": orig_size, "new_bytes": new_size,
            "video_seconds": video_duration or 0.0}


def diagnose_tagged_dst(src: Path, input_base: Path, output_folder: Path,
                         preset: str | None, encoding: str, crf: int) -> Path:
    """The name-tagged output path for a diagnostic encode of src under a given preset,
    landing flat-relative to input_base in output_folder as '<stem>_<preset>_crf<crf>'.
    Uses the effective preset name (resolving None to the encoder default) so default
    runs get a meaningful tag rather than 'None'."""
    rel_path = src.relative_to(input_base)
    dst = output_folder / rel_path
    effective_preset = preset or (DEFAULT_QSV_PRESET if encoding == "hardware"
                                  else DEFAULT_X265_PRESET)
    return dst.with_name(f"{dst.stem}_{effective_preset}_crf{crf}{dst.suffix}")


# Whether to write each ffmpeg command line to the log. On for normal batch runs, where
# the exact command is the record of how each archived file was produced; switched off
# by main() for --compare-crf and --diagnose, which are throwaway test runs where the
# commands are just noise around the comparison tables.
_LOG_FFMPEG_COMMANDS: bool = True


def log_ffmpeg_command(cmd: list[str], prefix: str = "") -> None:
    """Log an ffmpeg command line, unless command logging is switched off for this run
    (see _LOG_FFMPEG_COMMANDS)."""
    if _LOG_FFMPEG_COMMANDS:
        logging.info(f"{prefix}Command: {format_cmd_for_log(cmd)}")


def format_cmd_for_log(cmd: list[str]) -> str:
    """Join a subprocess argv list into a loggable command string, wrapping file/
    folder path arguments — the input path after -i, and a real output path at
    the end — in double quotes, so paths containing spaces are unambiguous when
    read back from the log (and the line can be copy-pasted into a shell as-is).
    Flags and their non-path values are left unquoted. The trailing "-" ffmpeg
    uses for a null/pipe output (as in the loudness measurement pass) is left
    unquoted too, since it isn't actually a path."""
    parts: list[str] = []
    for i, token in enumerate(cmd):
        is_input_path = i > 0 and cmd[i - 1] == "-i"
        is_output_path = (i == len(cmd) - 1) and token not in ("-", "pipe:", "pipe:1")
        if is_input_path or is_output_path:
            parts.append(f'"{token}"')
        else:
            parts.append(token)
    return " ".join(parts)


def build_ffmpeg_cmd(src: Path, dst: Path, crf: int, duration: float, needs_downscale: bool,
                      encoding: str = "software", normalize_audio: bool = True,
                      loudnorm_target: float = -16,
                      audio_stream_indices: list[int] | None = None,
                      video_stream_index: int = 0,
                      measured_loudness: dict[str, Any] | None = None,
                      preset: str | None = None,
                      deinterlace_filter: str | None = None,
                      audio_codec: str = DEFAULT_AUDIO_CODEC) -> list[str]:
    cmd: list[str] = ["ffmpeg", "-y", "-i", str(src)]

    if duration != -1:
        cmd += ["-t", str(duration)]

    audio_stream_indices = audio_stream_indices or []

    # Explicit stream mapping disables ffmpeg's automatic "best stream" selection,
    # so the video stream must always be mapped too. video_stream_index picks out
    # the real video track (as identified by probe_media) rather than assuming
    # it's the first video stream, since embedded cover art is also a video
    # stream and can precede the real one.
    cmd += ["-map", f"0:v:{video_stream_index}"]
    for idx in audio_stream_indices:
        cmd += ["-map", f"0:a:{idx}"]
    # Every subtitle track is carried through, unconditionally. Subtitles are a
    # rounding error against the video bitrate, and dropping one is irreversible, so
    # there's nothing to gain from choosing between them. The trailing '?' makes the
    # mapping optional, so a file with no subtitle streams at all isn't an error.
    cmd += ["-map", "0:s?"]

    # Video filters are one -vf chain, applied in order. Deinterlacing/inverse
    # telecine must come FIRST: both work on field structure, which only survives at
    # the source resolution — scaling first blends the two fields' scanlines together
    # and destroys the very comb pattern the filters need to see.
    video_filters: list[str] = []
    if deinterlace_filter:
        video_filters.append(deinterlace_filter)
    if needs_downscale:
        # Scale down so neither dimension exceeds 1080p, preserving aspect ratio.
        # force_original_aspect_ratio=decrease only shrinks, never upscales.
        # force_divisible_by=2 rounds the calculated dimension to an even number,
        # since x265/x264 require even width & height for 4:2:0 chroma subsampling.
        video_filters.append(
            "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2")
    if video_filters:
        cmd += ["-vf", ",".join(video_filters)]

    if encoding == "hardware":
        # Intel Quick Sync HEVC encoder. QSV uses -global_quality as its CRF-equivalent
        # quality knob rather than -crf. p010le is QSV's 10-bit pixel format.
        # veryslow + look_ahead (with an explicit depth) maximize quality-per-bit for
        # QSV. -low_power 0 forces the full-featured encode pipeline rather than the
        # fixed-function low-power path some Intel iGPUs default to, which can
        # otherwise silently ignore lookahead and other quality features.
        # global_quality:v (not the unscoped -global_quality) matters here: without a
        # stream specifier, ffmpeg's per-file option resolution applies the implied
        # "quality/CRF mode" flag to every output stream, not just the video one. QSV
        # handles that fine, but audio encoders don't: libopus refuses quality-scale
        # mode outright (aborting the whole encode with no output written), and the
        # aac encoder would silently switch from the fixed bitrate below to VBR.
        cmd += ["-pix_fmt", "p010le", "-c:v", "hevc_qsv", "-global_quality:v", str(crf),
                "-preset", preset or DEFAULT_QSV_PRESET, "-low_power", "0",
                "-look_ahead", "1", "-look_ahead_depth", "40"]
    else:
        # yuv420p10le: encode in 10-bit. Even for 8-bit sources, x265's finer
        # quantization steps in 10-bit mode noticeably improve compression
        # efficiency at a given CRF, at negligible compatibility cost on modern
        # players/decoders.
        cmd += ["-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-preset", preset or DEFAULT_X265_PRESET,
                "-crf", str(crf)]

    audio_encoder, audio_bitrate = AUDIO_CODEC_SETTINGS[audio_codec]
    # -ac 2 downmixes every track to stereo, folding the center (dialogue) and surround
    # channels into left/right.
    cmd += ["-c:a", audio_encoder, "-ac", "2", "-b:a", audio_bitrate]
    if audio_codec == "opus":
        # Make the downmix run in floating point. libopus also accepts 16-bit integer
        # audio, and with a 16-bit surround source (e.g. Blu-ray LPCM) ffmpeg picks
        # that, which scales the downmix down to avoid integer clipping: measured, a
        # 7.1 source's center channel came out 10 dB quieter than with AAC (-34.0 vs
        # -24.1 dB). Float has no clipping limit, so no scaling. Not needed for AAC,
        # whose encoder only accepts float anyway (and would reject this flag).
        cmd += ["-sample_fmt:a", "flt"]

    if normalize_audio:
        # Scoped to output audio stream 0 (-filter:a:0) rather than the unscoped -af,
        # which would apply this exact filter description — including the fixed
        # measured_* gain values below, which are only valid for the primary track —
        # identically to every kept audio stream. Only the primary/first mapped
        # audio track is normalized; any other kept tracks are still re-encoded with
        # the codec above, just without a loudness filter applied.
        #
        # loudnorm works internally at 192 kHz and outputs at that rate. libopus only
        # encodes at 48 kHz so it resamples back down by itself, but the aac encoder
        # accepts high rates and lands on 96 kHz (verified), wasting bitrate on
        # inaudible frequencies and risking playback trouble on some devices. Pinning
        # the normalized track to 48 kHz fixes that for both codecs.
        cmd += ["-ar:a:0", "48000"]
        if measured_loudness:
            # Two-pass EBU R128: feed the analysis pass's exact measured stats back
            # in with linear=true, which applies a single fixed gain rather than
            # loudnorm's single-pass dynamic (compressor-like) behavior. This is
            # more accurate but requires measure_loudness() to have already run.
            cmd += ["-filter:a:0", (
                f"loudnorm=I={loudnorm_target}:TP=-1.5:LRA=11:"
                f"measured_I={measured_loudness['input_i']}:"
                f"measured_TP={measured_loudness['input_tp']}:"
                f"measured_LRA={measured_loudness['input_lra']}:"
                f"measured_thresh={measured_loudness['input_thresh']}:"
                f"offset={measured_loudness['target_offset']}:"
                f"linear=true"
            )]
        else:
            # One-pass fallback: used for dry-run command previews (where we don't
            # want to actually run ffmpeg just to build a preview string) and for
            # real encodes where the measurement pass itself failed.
            cmd += ["-filter:a:0", f"loudnorm=I={loudnorm_target}:TP=-1.5:LRA=11"]

    # Passthrough: subtitle tracks are copied as-is, not re-encoded. Since the output
    # container always matches the input's (same file extension), the original subtitle
    # codec (subrip, ass, PGS, etc.) remains valid. Harmless on a file with no
    # subtitles, so it's set unconditionally alongside the blanket -map above.
    cmd += ["-c:s", "copy"]

    cmd += [str(dst)]
    return cmd


def process_file(src: Path, dst: Path, crf: int, duration: float, min_size_mb: float,
                  dry_run: bool = False, encoding: str = "software",
                  normalize_audio: bool = True, loudnorm_target: float = -16,
                  downscale: bool = False, strip_non_english_audio: bool = False,
                  preset: str | None = None,
                  processed_bytes_so_far: int = 0,
                  deinterlace: str = "off",
                  audio_codec: str = DEFAULT_AUDIO_CODEC) -> ProcessResult | None:
    """Returns (original_size, new_size, video_duration_seconds, action, downscaled,
    grew_larger, retried) on success or dry-run preview, or None only when the caller
    already decided to skip the file entirely before calling this (not used internally
    here; reserved for callers). action is 'encoded' or 'copied'. downscaled is True if
    the file was scaled down from >1080p. video_duration_seconds may be None if duration
    could not be determined at all. retried is True only when the real hardware encode
    needed more than one attempt to succeed (see HARDWARE_ENCODE_MAX_ATTEMPTS); always
    False for copied files, dry-run previews, and software encodes, since those are
    never retried. In dry-run mode, new_size is a placeholder equal to
    original_size (no actual encode happens, so the real output size is unknown) — safe
    because dry-run never prints the "Total reduction" summary that would otherwise
    misuse it; it's only used for the Encoded/Copied/Failed breakdown and --limit
    accounting, both of which need original_size and action/downscaled, not a real
    new_size. grew_larger is True whenever a real encode came out bigger than the
    source. Normally that encode is discarded in favor of copying the original through
    instead (action='copied', new_size==original_size), but when the file was
    downscaled the larger encode is KEPT (action='encoded', new_size>original_size),
    since falling back to the original would silently restore the >1080p resolution
    the user asked to reduce. Always False in dry-run mode, since no real encode
    happened to compare against, and always False when duration != -1, since a partial
    clip's size isn't comparable to the full source's. This whole check only applies
    to the normal batch path, not run_crf_comparison's test encodes, where seeing
    every CRF's actual size (including growth) is the point. Raises ConversionError if ffprobe or ffmpeg fails on a file
    that must be processed (small below-threshold files are copied regardless of a
    duration-probe failure, since they were never going to be encoded).

    processed_bytes_so_far is purely cosmetic: the running total (in bytes, across
    prior files in this batch) the caller reports alongside the pre-encode ENCODING/
    WOULD ENCODE log line, so someone tailing the log file can see cumulative progress
    without cross-referencing the console output. It defaults to 0 for callers (e.g.
    diagnose_encode_one's preset sweep) that don't track a meaningful running total.

    deinterlace selects the scan-type handling ('off', 'auto', 'deinterlace',
    'detelecine'); see resolve_deinterlace_filter. Files that are copied rather than
    encoded never run detection, since their video isn't touched.

    Every subtitle track in the source is carried through; there is no selection or
    language filtering."""
    min_size_bytes = min_size_mb * 1024 * 1024
    src_stat = src.stat()
    src_size = src_stat.st_size
    timeout_seconds = compute_timeout_seconds(src_size)

    if src_size < min_size_bytes:
        # Below the encode threshold, so we skip the full probe_media() call (codec,
        # audio/subtitle tracks, etc. are irrelevant to a file we're just copying).
        # We still grab duration with the lighter probe_duration() call so it's not
        # silently missing from the final "total video running time" summary.
        try:
            small_file_duration = probe_duration(src, timeout_seconds)
        except ConversionTimeoutError:
            # Propagate rather than shrugging it off: a probe that hangs on a file this
            # small is a stuck process, not a slow read, so the file is skipped and
            # logged as a timeout rather than silently copied.
            raise
        except ConversionError as e:
            logging.warning(f"Could not determine duration for below-threshold file "
                             f"(copying anyway): {e.reason}: {src}")
            small_file_duration = None

        if dry_run:
            logging.info(f"[DRY RUN] WOULD COPY (below {min_size_mb}MB minimum, "
                         f"{human_size(src_size)}): {src} -> {dst}")
            return (src_size, src_size, small_file_duration, "copied", False, False, False, False)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logging.info(f"COPIED (below {min_size_mb}MB minimum, "
                     f"{human_size(src_size)}): {src} -> {dst}")
        return (src_size, src_size, small_file_duration, "copied", False, False, False, False)

    media = probe_media(src, timeout_seconds)
    video_info = media["video"]
    codec = video_info.get("codec_name", "")
    width = video_info.get("width", 0)
    height = video_info.get("height", 0)
    video_duration = video_info.get("duration")

    if codec == "hevc":
        if dry_run:
            logging.info(f"[DRY RUN] WOULD COPY (already H.265): {src} -> {dst}")
            return (src_size, src_size, video_duration, "copied", False, False, False, False)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logging.info(f"COPIED (already H.265): {src} -> {dst}")
        return (src_size, src_size, video_duration, "copied", False, False, False, False)

    needs_downscale = downscale and (width > 1920 or height > 1080)

    audio_languages = media["audio_languages"]
    if audio_languages:
        track_list = ", ".join(f"{i}:{lang or 'und'}" for i, lang in enumerate(audio_languages))
        logging.info(f"AUDIO TRACKS FOUND ({len(audio_languages)}): {track_list}: {src}")
    else:
        logging.info(f"AUDIO TRACKS FOUND: none: {src}")

    subtitle_tracks = media["subtitle_tracks"]
    if subtitle_tracks:
        sub_list = ", ".join(f"{i}:{lang or 'und'} ({sub_codec})"
                              for i, (lang, sub_codec) in enumerate(subtitle_tracks))
        logging.info(f"SUBTITLE TRACKS FOUND ({len(subtitle_tracks)}): {sub_list}: {src}")
    else:
        logging.info(f"SUBTITLE TRACKS FOUND: none: {src}")

    if not strip_non_english_audio:
        keep_audio_indices = list(range(len(audio_languages)))
        if audio_languages:
            logging.info(f"AUDIO: strip-no-english-audio is off; keeping all "
                         f"{len(audio_languages)} track(s): {src}")
    elif audio_languages:
        primary_lang = audio_languages[0]
        if primary_lang in ("eng", "en"):
            keep_audio_indices = [i for i, lang in enumerate(audio_languages) if lang in ("eng", "en")]
            if not keep_audio_indices:
                keep_audio_indices = [0]  # safety net; shouldn't happen since primary is English
        else:
            keep_audio_indices = list(range(len(audio_languages)))

        kept_str = ", ".join(f"{i}:{audio_languages[i] or 'und'}" for i in keep_audio_indices)
        dropped = [i for i in range(len(audio_languages)) if i not in keep_audio_indices]
        dropped_str = ", ".join(f"{i}:{audio_languages[i] or 'und'}" for i in dropped) if dropped else "none"
        reason = (f"main track is English" if primary_lang in ("eng", "en")
                  else f"main track is not English (tagged '{primary_lang}')" if primary_lang
                  else "main track is untagged/unknown language")
        logging.info(f"AUDIO: {reason}; keeping [{kept_str}]; discarding [{dropped_str}]: {src}")
    else:
        keep_audio_indices = []

    # Subtitle/CC tracks are all carried through unconditionally; there is no
    # selection logic. See build_ffmpeg_cmd, which maps them with "0:s?".

    # Scan-type handling. Detection decodes a sample, so it only runs for files that
    # are actually being re-encoded — by this point the copy-through cases (already
    # HEVC, below the size threshold) have already returned.
    deinterlace_filter, deinterlace_note = resolve_deinterlace_filter(
        src, video_info, deinterlace)
    if deinterlace != "off":
        logging.info(f"SCAN TYPE: {deinterlace_note}: {src}")

    # With filtering off nothing has decoded a sample, so fall back to saying something
    # on the cheap evidence rather than staying silent — encoding interlaced video to
    # HEVC bakes the comb in permanently. The run summary counts these so a
    # library-wide pattern is visible without grepping the log.
    interlace_hint: str | None = None
    if deinterlace == "off":
        interlace_hint = container_interlace_hint(video_info)
        if interlace_hint:
            logging.warning(
                f"POSSIBLE INTERLACING ({interlace_hint}) but --deinterlace is off, so "
                f"the video is being encoded as-is. Metadata alone is weak evidence; "
                f"re-run this file with --deinterlace auto to check the picture "
                f"itself: {src}")

    log_action = "[DRY RUN] WOULD ENCODE" if dry_run else "ENCODING"
    scan_part = ""
    if deinterlace_filter:
        scan_part = (", scan=inverse-telecine" if deinterlace_filter == DETELECINE_FILTER
                     else ", scan=deinterlace")
    logging.info(f"{log_action}: {src} -> {dst} (codec={codec}, {width}x{height}, "
                 f"downscale={'yes' if needs_downscale else 'no'}{scan_part}, crf={crf}, "
                 f"encoding={encoding}, "
                 f"audio={audio_codec} {AUDIO_CODEC_SETTINGS[audio_codec][1]}, "
                 f"normalize_audio={'yes (' + str(loudnorm_target) + ' LUFS)' if normalize_audio else 'no'}, "
                 f"duration={'full' if duration == -1 else f'{duration}s'}, "
                 f"converted so far: {human_size(processed_bytes_so_far)})")

    if dry_run:
        # Dry runs never invoke ffmpeg, so there's no measured loudness stats to show
        # here; the preview command falls back to the one-pass filter shape. The real
        # encode (below) runs an actual two-pass measurement when normalize_audio is on.
        cmd = build_ffmpeg_cmd(src, dst, crf, duration, needs_downscale, encoding,
                                normalize_audio, loudnorm_target, keep_audio_indices,
                                video_info["stream_index"],
                                preset=preset, deinterlace_filter=deinterlace_filter,
                                audio_codec=audio_codec)
        log_ffmpeg_command(cmd, "[DRY RUN] ")
        if normalize_audio and keep_audio_indices:
            logging.info(f"[DRY RUN] Note: audio will be normalized in two passes "
                         f"(a measurement pass, then the exact-gain encode shown above "
                         f"reflects the one-pass shape only): {src}")
        # new_size is unknown without actually encoding, so src_size is reported as a
        # placeholder (no reduction assumed). This is safe because the "Total
        # reduction" summary is never printed in dry-run mode, only the
        # Encoded/Copied/Failed breakdown and --limit accounting, which need orig_size
        # and action/downscaled, not a real new_size.
        return (src_size, src_size, video_duration, "encoded", needs_downscale, False, False,
                bool(interlace_hint))

    # Output is always a separate path from the source, so ffmpeg can write straight to
    # dst — no temp-file-and-swap dance (which only existed to avoid reading and writing
    # the same file during in-place conversion).
    dst.parent.mkdir(parents=True, exist_ok=True)

    measured_loudness = None
    if normalize_audio and keep_audio_indices:
        try:
            measured_loudness = measure_loudness(src, duration, loudnorm_target,
                                                  keep_audio_indices[0])
            logging.info(f"LOUDNESS MEASURED (pass 1): input {measured_loudness.get('input_i')} LUFS "
                         f"-> target {loudnorm_target} LUFS: {src}")
        except ConversionError as e:
            logging.warning(f"Loudness measurement pass failed, falling back to "
                             f"one-pass normalization for this file: {e.reason}: {src}")
            measured_loudness = None

    cmd = build_ffmpeg_cmd(src, dst, crf, duration, needs_downscale, encoding,
                            normalize_audio, loudnorm_target, keep_audio_indices,
                            video_info["stream_index"],
                            measured_loudness, preset=preset,
                            deinterlace_filter=deinterlace_filter,
                            audio_codec=audio_codec)
    cmd = add_progress_flags(cmd)
    log_ffmpeg_command(cmd)
    max_attempts = HARDWARE_ENCODE_MAX_ATTEMPTS if encoding == "hardware" else 1
    for attempt in range(1, max_attempts + 1):
        result = run_ffmpeg_with_watchdog(cmd)
        encode_elapsed = result.elapsed

        if result.returncode == 0 and not result.stalled:
            break

        if dst.exists():
            dst.unlink(missing_ok=True)

        # A watchdog kill is reported as its own failure kind rather than a bare exit
        # code, since "no output and no CPU for N minutes" needs a different diagnosis
        # from a normal ffmpeg error. It follows the same retry policy: a wedged Quick
        # Sync session is exactly the transient fault retrying is meant to absorb.
        if result.stalled:
            failure_desc = (f"stalled (no progress and no CPU activity for "
                            f"{human_duration(result.silent_for, include_seconds=True)}, "
                            f"killed after "
                            f"{human_duration(result.elapsed, include_seconds=True)})")
        else:
            failure_desc = f"exited with code {result.returncode}"

        if attempt < max_attempts:
            logging.warning(f"ffmpeg {failure_desc} on attempt "
                             f"{attempt}/{max_attempts} (often a transient hardware "
                             f"encoder hiccup from back-to-back Quick Sync sessions); "
                             f"retrying in {HARDWARE_ENCODE_RETRY_DELAY_SECONDS}s: "
                             f"{src}\n{result.stderr[-2000:]}")
            time.sleep(HARDWARE_ENCODE_RETRY_DELAY_SECONDS)
        else:
            attempts_str = f"{max_attempts} attempt(s)" if max_attempts > 1 else "1 attempt"
            raise ConversionError(src, f"ffmpeg {failure_desc} "
                                        f"after {attempts_str}: {result.stderr[-2000:]}")

    # Post-encode validation: confirm the output's duration roughly matches the
    # source's before trusting it. Skipped for test/partial encodes (--duration), since those are
    # intentionally shorter than the source.
    if duration == -1 and video_duration is not None:
        output_duration = probe_duration(dst, timeout_seconds)
        if output_duration is None:
            if dst.exists():
                dst.unlink(missing_ok=True)
            raise ConversionError(src, "post-encode validation failed: could not "
                                        "determine output duration")
        tolerance = max(2.0, 0.02 * video_duration)
        if abs(output_duration - video_duration) > tolerance:
            if dst.exists():
                dst.unlink(missing_ok=True)
            raise ConversionError(src, f"post-encode validation failed: source duration "
                                        f"{video_duration:.1f}s vs output duration "
                                        f"{output_duration:.1f}s (tolerance {tolerance:.1f}s)")
        logging.info(f"VALIDATED: output duration {output_duration:.1f}s matches source "
                     f"{video_duration:.1f}s: {src}")

    # Check the candidate output's size before committing it, so a converted file that
    # ended up larger than the source is never kept — growing storage instead of
    # shrinking it defeats the point of this script. Skipped when --duration is set: the
    # candidate is only the first N seconds, so comparing its size against the full
    # source's is meaningless (a short clip of a big file always "shrinks"; a clip of a
    # tiny source could spuriously "grow" and get replaced by a full-length copy of the
    # original, which isn't the test output the user asked for). Also skipped when the
    # file was downscaled: falling back to the original would silently restore the
    # >1080p resolution the user explicitly asked to reduce, so a size regression is the
    # lesser surprise there. --compare-crf deliberately doesn't apply this either:
    # seeing every CRF's real size, including ones that grew, is the whole point of
    # that comparison.
    candidate_size = dst.stat().st_size
    if duration == -1 and not needs_downscale and candidate_size > src_size:
        growth_pct = (candidate_size / src_size - 1) * 100 if src_size else 0
        shutil.copy2(src, dst)  # overwrites the too-large candidate at dst
        logging.warning(f"DISCARDED (encode grew {human_size(src_size)} -> "
                        f"{human_size(candidate_size)}, +{growth_pct:.1f}%); "
                        f"copied original instead: {src} -> {dst}")
        return (src_size, src_size, video_duration, "copied", False, True, False,
                bool(interlace_hint))

    # Preserve the source file's modification/access time on the newly encoded output.
    os.utime(dst, (src_stat.st_atime, src_stat.st_mtime))

    orig_size = src_size
    new_size = dst.stat().st_size if dst.exists() else 0
    saved_pct = (1 - new_size / orig_size) * 100 if orig_size else 0

    # A downscaled encode is exempt from the discard-on-growth check above, but a size
    # regression is still worth surfacing rather than passing silently.
    grew_larger = new_size > orig_size
    if grew_larger:
        logging.warning(f"LARGER AFTER ENCODING (kept anyway — file was downscaled to "
                        f"1080p, so falling back to the original would undo that): "
                        f"{human_size(orig_size)} -> {human_size(new_size)}: {dst}")

    # Realtime factor: how many seconds of video were encoded per second of wall clock.
    # Uses the encoded span (the --duration clip length when set, else the full source
    # duration), so a partial encode isn't credited with the whole file's runtime. Only
    # shown when both numbers are known and the encode took measurable time.
    encoded_span = video_duration if duration == -1 else min(duration, video_duration or duration)
    if encoded_span and encode_elapsed > 0:
        speed_str = f", {human_duration(encode_elapsed, include_seconds=True)} @ {encoded_span / encode_elapsed:.1f}x realtime"
    else:
        speed_str = f", {human_duration(encode_elapsed, include_seconds=True)}"

    logging.info(f"DONE: {src} -> {dst} "
                 f"({human_size(orig_size)} -> {human_size(new_size)}, {saved_pct:.1f}% smaller"
                 f"{speed_str})")
    return (orig_size, new_size, video_duration, "encoded", needs_downscale, grew_larger,
             attempt > 1, bool(interlace_hint))


def run_crf_comparison(src: Path, output_folder: Path, crf_values: list[int], duration: float,
                        encoding: str, downscale: bool,
                        preset: str | None = None,
                        deinterlace: str = "off",
                        audio_codec: str = DEFAULT_AUDIO_CODEC) -> tuple[int, list[CrfRow]]:
    """Comparison mode for a single source file: test-encodes src once per CRF value in
    crf_values, all other settings held fixed (audio normalization off, all audio/
    subtitle tracks kept), and prints/logs a size + encode-time table so the effect
    of --crf alone is easy to read off, whether encoding is hardware or software. A CRF
    value whose output file already exists is not re-encoded; its existing size is
    reused instead. Also copies the unmodified original into output_folder (trimmed to
    match via lossless stream copy when duration is set) so it can be compared side by
    side with every CRF variant — skipped if that copy already exists from a previous
    run, since it's identical regardless of encoding/CRF and doesn't need to be redone.
    Called once per file by main() when --compare-crf covers a whole folder; output
    filenames use src.stem and include the encoding type, so hardware vs. software
    re-runs of the same file coexist in one output folder without colliding. Note that
    two sources sharing a basename in different subfolders WOULD collide here, and the
    second would be silently treated as already-encoded; comparison mode is intended
    for a flat folder of test files, where that can't arise.
    Returns (src_size, rows), where rows is the same (crf, size_bytes_or_None,
    elapsed_seconds, error_or_None, skipped) list used for this file's own table, so
    main() can fold every file's rows together into one aggregate table across the
    whole batch. skipped is True when that CRF's output already existed and wasn't
    re-encoded (elapsed is 0.0 in that case, excluded from the aggregate's avg time)."""
    output_folder.mkdir(parents=True, exist_ok=True)

    src_size = src.stat().st_size
    timeout_seconds = compute_timeout_seconds(src_size)

    clip_label = "full file" if duration == -1 else \
        f"{human_duration(duration, include_seconds=True)} test clip"
    # The effective preset (resolving "not given" to the encoder's default) goes into
    # every output name, so comparisons run under different presets land side by side
    # instead of colliding, or being skipped as "already exists".
    effective_preset = preset or (DEFAULT_QSV_PRESET if encoding == "hardware"
                                  else DEFAULT_X265_PRESET)
    header = (f"CRF comparison for: {src.name}  ({clip_label}, {encoding} encoding, "
              f"preset {effective_preset})")
    print(f"\n{header}")
    logging.info(header)

    # Also place the unmodified original alongside the CRF variants, so all of them can
    # be compared side by side (visually and by size). When --duration trims the CRF
    # test encodes to a short clip, the original is trimmed to match via a lossless
    # stream copy (no re-encode) rather than copying the full multi-GB source, which
    # would defeat the point of a quick test; with no --duration, it's a plain byte-for-
    # byte copy. Failure here is a warning, not fatal — the CRF comparison itself
    # doesn't depend on it.
    original_dst = output_folder / f"{src.stem}_original{src.suffix}"
    original_copied = False
    if original_dst.exists():
        print(f"  Skipping original copy: already exists ({original_dst.name})")
        logging.info(f"Original comparison copy already exists, skipping: {original_dst}")
        original_copied = True
    else:
        try:
            if duration == -1:
                shutil.copy2(src, original_dst)
            else:
                copy_cmd = add_progress_flags(
                    ["ffmpeg", "-y", "-i", str(src), "-t", str(duration),
                     "-c", "copy", str(original_dst)])
                log_ffmpeg_command(copy_cmd)
                copy_result = run_ffmpeg_with_watchdog(copy_cmd)
                if copy_result.stalled:
                    raise RuntimeError(f"ffmpeg stalled (no progress and no CPU activity "
                                        f"for {human_duration(copy_result.silent_for, include_seconds=True)})")
                if copy_result.returncode != 0 or not original_dst.exists():
                    raise RuntimeError(f"ffmpeg exited with code {copy_result.returncode}: "
                                        f"{copy_result.stderr[-500:]}")
            logging.info(f"COPIED (original, unmodified): {src} -> {original_dst}")
            original_copied = True
        except (OSError, RuntimeError) as e:
            logging.warning(f"Could not create original-comparison copy, skipping it: {e}: {src}")

    media = probe_media(src, timeout_seconds)
    video_info = media["video"]
    width, height = video_info.get("width", 0), video_info.get("height", 0)
    needs_downscale = downscale and (width > 1920 or height > 1080)
    keep_audio_indices = list(range(len(media["audio_languages"])))

    # Detection runs once, not once per CRF: the scan type is a property of the source,
    # and every variant in the table must share it or the sizes aren't comparable.
    deinterlace_filter, deinterlace_note = resolve_deinterlace_filter(
        src, video_info, deinterlace)
    if deinterlace != "off":
        logging.info(f"SCAN TYPE: {deinterlace_note}: {src}")
        print(f"  Scan type: {deinterlace_note}")

    rows: list[CrfRow] = []  # (crf, size_bytes_or_None, elapsed_seconds, error_or_None, skipped)
    for crf in crf_values:
        dst = output_folder / f"{src.stem}_crf{crf}_{encoding}_{effective_preset}{src.suffix}"

        if dst.exists():
            print(f"  Skipping CRF {crf}: output already exists ({dst.name})")
            logging.info(f"CRF {crf}: output already exists, skipping encode: {dst}")
            rows.append((crf, dst.stat().st_size, 0.0, None, True))
            continue

        cmd = build_ffmpeg_cmd(src, dst, crf, duration, needs_downscale, encoding,
                                False, -16, keep_audio_indices,
                                video_info["stream_index"], None, preset=preset,
                                deinterlace_filter=deinterlace_filter,
                                audio_codec=audio_codec)
        cmd = add_progress_flags(cmd)
        log_ffmpeg_command(cmd)

        result = run_ffmpeg_with_watchdog(cmd)
        elapsed = result.elapsed

        if result.stalled:
            logging.error(f"CRF {crf}: ffmpeg stalled (no progress and no CPU activity "
                          f"for {human_duration(result.silent_for, include_seconds=True)}), "
                          f"killed after {human_duration(elapsed, include_seconds=True)}: "
                          f"{src}")
            if dst.exists():
                dst.unlink(missing_ok=True)
            rows.append((crf, None, elapsed, "stalled", False))
            continue

        if result.returncode != 0 or not dst.exists():
            logging.error(f"CRF {crf}: ffmpeg failed (exit {result.returncode}): "
                          f"{result.stderr[-500:]}")
            rows.append((crf, None, elapsed, "failed", False))
            continue

        rows.append((crf, dst.stat().st_size, elapsed, None, False))

    lines = [f"{'CRF':>5}  {'Size':>10}  {'% Reduction':>13}  {'Time':>8}"]
    for crf, size, elapsed, err, skipped in rows:
        if err is not None:
            lines.append(f"{crf:>5}  {err:>10}  {'--':>13}  "
                         f"{human_duration(elapsed, include_seconds=True):>8}")
        else:
            assert size is not None  # a non-error row always carries a size
            pct = f"{(src_size - size) / src_size * 100:.1f}%" if src_size else "--"
            time_col = "existing" if skipped else human_duration(elapsed, include_seconds=True)
            lines.append(f"{crf:>5}  {human_size(size):>10}  {pct:>13}  {time_col:>8}")

    table = "\n".join(lines)
    print(table)
    if original_copied:
        print(f"Original (unmodified) available at: {original_dst}")
    print(f"\nTest files written to: {output_folder}")
    for line in lines:
        logging.info(line)

    return (src_size, rows)


def print_crf_aggregate_summary(aggregate: dict[int, dict[str, float]],
                                 crf_values: list[int], files_compared: int) -> None:
    """Prints/logs one summary table folding every file's CRF comparison together, so
    the best CRF for a whole batch of varied content is easy to read off in one place
    rather than eyeballing each file's individual table. aggregate maps crf -> {orig,
    new, time, ok, failed, timed_ok} as accumulated by main(); % reduction is computed
    over every success (ok), while avg time is computed only over timed_ok (freshly
    encoded files, excluding ones that reused an existing output) so a skipped-existing
    file's elapsed=0 doesn't drag the average down. failed/timed-out attempts are
    reported as a count, not folded into the size/time totals, since they contributed
    no size or a meaningless partial time."""
    header: str = f"\nAggregate CRF comparison across {files_compared} file(s):"
    print(header)
    logging.info(header.strip())

    lines: list[str] = [f"{'CRF':>5}  {'Files OK':>8}  {'Total Size':>11}  "
                        f"{'% Reduction':>13}  {'Avg Time':>9}"]
    for crf in crf_values:
        stats = aggregate[crf]
        files_str = f"{stats['ok']}/{files_compared}"
        if stats["ok"] == 0:
            lines.append(f"{crf:>5}  {files_str:>8}  {'--':>11}  {'--':>13}  {'--':>9}")
            continue
        pct = f"{(stats['orig'] - stats['new']) / stats['orig'] * 100:.1f}%" if stats["orig"] else "--"
        if stats["timed_ok"] > 0:
            avg_time = human_duration(stats["time"] / stats["timed_ok"], include_seconds=True)
        else:
            avg_time = "--"  # every success for this CRF reused an existing output
        lines.append(f"{crf:>5}  {files_str:>8}  {human_size(stats['new']):>11}  "
                     f"{pct:>13}  {avg_time:>9}")

    table = "\n".join(lines)
    print(table)
    for line in lines:
        logging.info(line)

    if any(aggregate[crf]["failed"] for crf in crf_values):
        note = ("Note: 'Files OK' excludes failed/timed-out encodes at that CRF; see "
                "the per-file tables above and the log for details.")
        print(f"\n{note}")
        logging.info(note)


# Set once logging is configured, so the end-of-script banner knows whether there's a
# log file to write to and how long the run took. Argument errors exit before logging
# is set up (the output folder may not even be valid yet), so those runs log nothing.
_SCRIPT_START_MONOTONIC: float | None = None

# Set by main() when a run ends early but still returns normally (--limit reached,
# or the consecutive-failure circuit breaker), so the end banner doesn't report a
# cut-short run as a plain 'completed'.
_SCRIPT_STOP_NOTE: str | None = None

LOG_BANNER_RULE: str = "=" * 78


def _ffmpeg_version_line() -> str:
    """First line of `ffmpeg -version`, for the start banner, or a placeholder if it
    can't be read. Recording it makes a months-long archive log self-describing: if
    output quality or behaviour ever shifts, the log shows which build produced it."""
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                                timeout=30)
        first = result.stdout.splitlines()[0] if result.stdout else ""
        return first.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return "unavailable"


def log_script_start() -> None:
    """Write the start-of-run banner to the log file. The log is opened in append mode,
    so successive runs accumulate in one file; the ruled banner makes each run's
    boundary easy to find when scrolling or searching. INFO level, so it goes to the
    log file only, never the console."""
    global _SCRIPT_START_MONOTONIC
    _SCRIPT_START_MONOTONIC = time.monotonic()
    logging.info(LOG_BANNER_RULE)
    logging.info(f"SCRIPT STARTED at {time.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                 f"(pid {os.getpid()})")
    logging.info(f"Command line: {subprocess.list2cmdline(sys.argv)}")
    logging.info(f"Python {sys.version.split()[0]}; {_ffmpeg_version_line()}")
    logging.info(LOG_BANNER_RULE)


def log_script_end(status: str, crash_details: str | None = None) -> None:
    """Write the end-of-run banner to the log file, whatever way the script ended:
    normal completion, an explicit exit, Ctrl-C, or an unhandled crash (in which case
    the traceback is written too, so the log alone is enough to diagnose it). A no-op
    when logging was never set up. Never raises — it runs on the way out of an
    exception, and must not replace the original error with a new one."""
    if _SCRIPT_START_MONOTONIC is None:
        return
    try:
        runtime = time.monotonic() - _SCRIPT_START_MONOTONIC
        if crash_details:
            logging.info(f"Unhandled exception:\n{crash_details.rstrip()}")
        logging.info(LOG_BANNER_RULE)
        logging.info(f"SCRIPT FINISHED at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}: "
                     f"{status}; ran for "
                     f"{human_duration(runtime, include_seconds=True)}")
        logging.info(LOG_BANNER_RULE)
        logging.shutdown()  # flush the file handler before the process exits
    except Exception:
        pass


def main() -> None:
    global _SCRIPT_STOP_NOTE, _LOG_FFMPEG_COMMANDS
    args = parse_args()

    video_extensions: tuple[str, ...] = ("*.mp4", "*.MP4", "*.mkv", "*.MKV")

    # Input and output must be different folders (and, checked further below, neither
    # nested in the other). Both default to the current folder, so requiring at least
    # one to be given explicitly is what prevents them both silently falling back to
    # "." and colliding. Fill in the current folder for whichever wasn't provided.
    if args.input_folder is None and args.output_folder is None:
        print("Error: at least one of -i/--input or -o/--output must be given. They "
              "default to the current folder, and the input and output folders must "
              "be different, so they can't both be left to default.", file=sys.stderr)
        sys.exit(1)
    if args.input_folder is None:
        args.input_folder = Path(".")
    if args.output_folder is None:
        args.output_folder = Path(".")

    # -i may name either a folder (recurse into it, as usual) or a single video file.
    # If a name is somehow BOTH a directory and a file on disk, the directory wins
    # (documented precedence). input_base is the folder that output paths are made
    # relative to: the input folder itself for a folder run, or the file's parent for
    # a single-file run (so its output lands flat in the output folder, no structure to
    # mirror).
    input_base: Path
    mp4_files: list[Path]
    if args.input_folder.is_dir():
        input_base = args.input_folder
        mp4_files = sorted(set().union(*(input_base.rglob(pat) for pat in video_extensions)))
        if not mp4_files:
            print(f"No .mp4 or .mkv files found in: {args.input_folder}\n", file=sys.stderr)
            build_parser().print_help()
            sys.exit(1)
    elif args.input_folder.is_file():
        if args.input_folder.suffix.lower() not in (".mp4", ".mkv"):
            print(f"Input file is not a .mp4 or .mkv file: {args.input_folder}",
                  file=sys.stderr)
            sys.exit(1)
        input_base = args.input_folder.parent
        mp4_files = [args.input_folder]
    else:
        print(f"Input path does not exist (expected a folder or a .mp4/.mkv file): "
              f"{args.input_folder}", file=sys.stderr)
        sys.exit(1)

    if args.preset is not None:
        valid_presets = QSV_PRESETS if args.encoding == "hardware" else X265_PRESETS
        if args.preset not in valid_presets:
            print(f"Error: --preset {args.preset!r} is not valid for "
                  f"--encoding={args.encoding}. Valid values: "
                  f"{', '.join(valid_presets)}.", file=sys.stderr)
            sys.exit(1)

    include_re: re.Pattern | None = None
    exclude_re: re.Pattern | None = None
    try:
        if args.include is not None:
            include_re = re.compile(args.include, re.IGNORECASE)
        if args.exclude is not None:
            exclude_re = re.compile(args.exclude, re.IGNORECASE)
    except re.error as e:
        print(f"Error: invalid --include/--exclude regex: {e}", file=sys.stderr)
        sys.exit(1)

    if include_re is not None or exclude_re is not None:
        total_found = len(mp4_files)
        mp4_files = filter_files(mp4_files, input_base, include_re, exclude_re)
        filtered_out = total_found - len(mp4_files)
        # Printed before logging is configured, so echo to the console directly; the
        # same numbers are logged again below once the log file is open.
        print(f"Filtered {total_found} file(s) down to {len(mp4_files)} "
              f"({filtered_out} excluded by --include/--exclude).")
        if not mp4_files:
            print("No files remain after applying --include/--exclude; nothing to do.",
                  file=sys.stderr)
            sys.exit(1)

    crf_values: list[int] | None = None
    if args.compare_crf is not None:
        try:
            crf_values = sorted({int(v.strip()) for v in args.compare_crf.split(",") if v.strip()})
        except ValueError:
            print(f"Error: --compare-crf must be a comma-separated list of integers, "
                  f"got: {args.compare_crf!r}", file=sys.stderr)
            sys.exit(1)
        if len(crf_values) < 2:
            print("Error: --compare-crf needs at least two distinct CRF values to compare.",
                  file=sys.stderr)
            sys.exit(1)

    # The input and output folders must be different and neither may be nested inside
    # the other. input_base is what to compare — not the raw -i argument, which for a
    # single-file run is the file itself; input_base is that file's parent, the folder
    # its output is written into. Enforcing this for every run (not just per-flag)
    # guarantees a run never overwrites a source, and never rescans its own output on a
    # later pass (which recursion would otherwise pick up as new input).
    input_resolved = input_base.resolve()
    output_resolved = args.output_folder.resolve()

    if input_resolved == output_resolved:
        print(f"Error: the input and output folders must be different, but both resolve "
              f"to {output_resolved}. Give a different -o/--output (or -i/--input).",
              file=sys.stderr)
        sys.exit(1)

    if _is_within(output_resolved, input_resolved):
        print(f"Error: the output folder ({output_resolved}) is inside the input folder "
              f"({input_resolved}). They must not be nested, or a recursive scan would "
              f"pick up already-converted files as new input. Choose an output folder "
              f"outside the input tree.", file=sys.stderr)
        sys.exit(1)

    if _is_within(input_resolved, output_resolved):
        print(f"Error: the input folder ({input_resolved}) is inside the output folder "
              f"({output_resolved}). They must not be nested. Choose an output folder "
              f"outside the input tree.", file=sys.stderr)
        sys.exit(1)

    # Comparison runs get an encoder-tagged log, so a hardware and a software run over
    # the same output folder produce separate logs rather than interleaving in one.
    log_suffix = f"_{args.encoding}" if crf_values is not None else ""
    log_path = setup_logging(args.output_folder, log_suffix)
    log_script_start()

    # Test modes don't need a per-file command trail; the comparison tables are the
    # output that matters there.
    if crf_values is not None or args.diagnose:
        _LOG_FFMPEG_COMMANDS = False
        logging.info("ffmpeg command logging is off for this run "
                     f"({'--compare-crf' if crf_values is not None else '--diagnose'}).")

    # --source filtering. Runs at startup rather than per file so the file count, total
    # size, ETA and --limit all describe only the chosen group. Done after logging is
    # set up so unreadable files are recorded in the log.
    if args.source != "all":
        wanted = SOURCE_LABELS[args.source]
        print(f"Sorting {len(mp4_files)} file(s) by resolution to select {wanted} "
              f"sources...")
        kept: list[Path] = []
        other_count = 0
        unreadable: list[Path] = []
        for src in mp4_files:
            classified = classify_source(src)
            if classified is None:
                unreadable.append(src)
            elif classified[0] == args.source:
                kept.append(src)
            else:
                other_count += 1
        other_label = SOURCE_LABELS["bd" if args.source == "dvd" else "dvd"]
        summary = (f"Source filter --source {args.source}: {len(kept)} {wanted} file(s) "
                   f"selected; {other_count} {other_label} file(s) skipped")
        if unreadable:
            summary += f"; {len(unreadable)} file(s) skipped as unreadable"
        logging.info(summary)
        print(summary)
        for src in unreadable:
            # Unreadable files fit neither group, so a dvd run and a bd run would both
            # pass over them silently. Warn (log + console) so they don't go unnoticed.
            logging.warning(f"Skipped by --source: could not read resolution, so this "
                            f"file is neither DVD nor Blu-ray here. Run with --source all "
                            f"to process it (and see the error): {src}")
        if not kept:
            logging.info(f"No {wanted} files to process; nothing to do.")
            print(f"No {wanted} files found; nothing to do.", file=sys.stderr)
            sys.exit(1)
        mp4_files = kept

    if crf_values is not None:
        compare_start = time.monotonic()
        logging.info(f"CRF comparison mode: {len(mp4_files)} file(s) found in "
                     f"{input_resolved}, values: {crf_values}")
        # Per-CRF totals across every file, so a batch of files can be compared as a
        # whole rather than only reading each file's own table individually. Only
        # successful encodes (err is None) contribute to orig/new/ok; a CRF value that
        # fails or times out on a file still gets counted in "failed" for that CRF.
        # "timed_ok" tracks only freshly-encoded successes (not reused existing output),
        # so a skipped-existing file's elapsed=0 doesn't drag down the avg-time column.
        aggregate: dict[int, dict[str, float]] = {
            crf: {"orig": 0, "new": 0, "time": 0.0, "ok": 0, "failed": 0, "timed_ok": 0}
            for crf in crf_values}
        files_compared = 0
        total_files = len(mp4_files)
        for index, src in enumerate(mp4_files, start=1):
            logging.info(f"[{index}/{total_files}] Processing: {src.name}")
            try:
                src_size, rows = run_crf_comparison(src, args.output_folder, crf_values,
                                                     args.duration, args.encoding,
                                                     args.downscale, args.preset,
                                                     args.deinterlace, args.audio_codec)
            except ConversionError as e:
                # As in the main batch loop, a timeout is logged distinctly but skips
                # the file rather than ending the run.
                if isinstance(e, ConversionTimeoutError):
                    logging.error(f"TIMEOUT: {e.file.resolve()}\n{e.reason}")
                else:
                    logging.error(f"CRF comparison skipped for {e.file.resolve()}: {e.reason}")
                continue

            files_compared += 1
            for crf, size, elapsed, err, skipped in rows:
                if err is None:
                    assert size is not None  # a non-error row always carries a size
                    aggregate[crf]["orig"] += src_size
                    aggregate[crf]["new"] += size
                    aggregate[crf]["ok"] += 1
                    if not skipped:
                        aggregate[crf]["time"] += elapsed
                        aggregate[crf]["timed_ok"] += 1
                else:
                    aggregate[crf]["failed"] += 1

        if files_compared > 1:
            print_crf_aggregate_summary(aggregate, crf_values, files_compared)

        compare_runtime = time.monotonic() - compare_start
        runtime_line = (f"Script runtime: "
                        f"{human_duration(compare_runtime, include_seconds=True)}")
        logging.info(runtime_line)
        print(f"\n{runtime_line}")
        print(f"Log written to: {log_path}")

        sys.exit(0)

    mode = "DRY RUN" if args.dry_run else "LIVE"
    effective_preset = args.preset or (DEFAULT_QSV_PRESET if args.encoding == "hardware"
                                        else DEFAULT_X265_PRESET)
    logging.info(f"Starting batch conversion [{mode}]. Input: {input_resolved} "
                 f"Output: {output_resolved} CRF: {args.crf} "
                 f"Encoding: {args.encoding} Preset: {effective_preset} "
                 f"Normalize audio: {'yes (' + str(args.loudnorm_target) + ' LUFS)' if args.normalize_audio else 'no'} "
                 f"Duration limit: {'none' if args.duration == -1 else f'{args.duration}s'} "
                 f"Min size: {args.min_size_mb}MB "
                 f"Data limit: {'none' if args.limit == -1 else f'{args.limit}GB'} "
                 f"Downscale to 1080p: {'yes' if args.downscale else 'no'} "
                 f"Strip non-English audio: {'yes' if args.strip_no_english_audio else 'no'} "
                 f"Source: {args.source} "
                 f"Deinterlace: {args.deinterlace} "
                 f"Audio: {args.audio_codec} {AUDIO_CODEC_SETTINGS[args.audio_codec][1]} stereo")

    if args.include is not None or args.exclude is not None:
        logging.info(f"Filters active — include: {args.include!r}, "
                     f"exclude: {args.exclude!r} (matched against each file's path "
                     f"relative to the input folder, forward-slash form, "
                     f"case-insensitive, start-anchored).")

    logging.info(f"Found {len(mp4_files)} .mp4/.mkv file(s) to process.")

    total_orig: float = 0
    total_new: float = 0
    total_duration_seconds = 0.0
    failed_files: list[Path] = []
    skipped_existing = 0
    encoded_count = 0
    copied_count = 0
    downscaled_count = 0
    grew_larger_count = 0
    retried_count = 0
    # Files encoded with --deinterlace off whose container metadata claimed interlaced
    # video. Advisory only — see container_interlace_hint.
    interlace_hinted_count = 0
    # Informational subtitle accounting, reported to the log file only.
    subtitle_bytes_total = 0
    subtitle_tracks_total = 0
    # Counts failures back-to-back, reset by any file that succeeds. Drives the
    # circuit breaker that distinguishes a run of bad files from a systemic fault.
    consecutive_failures = 0
    # Diagnostic accumulators (only populated when --diagnose is set).
    diag_read_seconds = 0.0
    diag_read_bytes = 0
    diag_encode_seconds = 0.0
    diag_encode_bytes: float = 0
    diag_encode_new_bytes: float = 0
    diag_video_seconds = 0.0

    # When --diagnose is set WITHOUT an explicit --preset, sweep every preset for the
    # active encoder so they can be compared head-to-head; otherwise a single preset
    # runs (the explicit one, or the encoder default via None). diag_sweep flags the
    # multi-preset case, which produces the comparison table instead of the single
    # bottleneck block. preset_stats accumulates per-preset numbers for the table.
    presets_to_test: list[str | None]
    if args.diagnose and args.preset is None:
        presets_to_test = list(QSV_PRESETS if args.encoding == "hardware" else X265_PRESETS)
        diag_sweep = True
    else:
        presets_to_test = [args.preset]
        diag_sweep = False
    preset_stats: PresetStats = {p: {"enc_seconds": 0.0, "enc_bytes": 0, "new_bytes": 0,
                                     "video_seconds": 0.0, "files": 0} for p in presets_to_test}

    limit_bytes: float = float("inf") if args.limit == -1 else args.limit * 1024 ** 3
    limit_reached = False

    total_files = len(mp4_files)
    total_size_bytes = sum(f.stat().st_size for f in mp4_files)
    processed_bytes = 0
    batch_start = time.monotonic()

    for index, src in enumerate(mp4_files, start=1):
        src_size = src.stat().st_size
        remaining_bytes = total_size_bytes - processed_bytes

        # Check the limit BEFORE starting this file, so it acts as a true ceiling
        # rather than being overshot by up to one file's size. total_orig only counts
        # files actually processed (not ones skipped for an existing output), matching
        # what --limit is meant to cap.
        if total_orig + src_size > limit_bytes:
            limit_reached = True
            _SCRIPT_STOP_NOTE = f"stopped early at the --limit of {args.limit}GB"
            verb = "would be processed" if args.dry_run else "processed"
            logging.info(f"Data limit of {args.limit}GB reached: next file "
                         f"({human_size(src_size)}) would exceed it "
                         f"({human_size(total_orig)} {verb} so far). Stopping.")
            index -= 1  # this file wasn't started, so it counts as unprocessed below
            break

        elapsed_so_far = time.monotonic() - batch_start
        if processed_bytes > 0 and elapsed_so_far > 0:
            rate = processed_bytes / elapsed_so_far  # bytes/sec
            eta_str = human_duration(remaining_bytes / rate) if rate > 0 else "unknown"
        else:
            eta_str = "calculating..."

        rel_path = src.relative_to(input_base)
        dst = args.output_folder / rel_path
        # In diagnostic mode, tag the output name with the preset and CRF so the same
        # source encoded under different settings lands in distinct files (rather than
        # colliding, or being skipped as "already exists"), making A/B comparison of
        # presets/CRF levels straightforward.
        if args.diagnose:
            effective_preset = args.preset or (DEFAULT_QSV_PRESET
                                                if args.encoding == "hardware"
                                                else DEFAULT_X265_PRESET)
            dst = dst.with_name(f"{dst.stem}_{effective_preset}_crf{args.crf}{dst.suffix}")

        if dst.exists() and not args.force:
            print(f"[{time.strftime('%H:%M:%S')}] Output file exists: {dst}")
            prefix = "[DRY RUN] WOULD SKIP" if args.dry_run else "SKIPPED"
            logging.info(f"{prefix} (output file already exists): {dst}")
            skipped_existing += 1
            processed_bytes += src_size
            continue

        print(f"[{time.strftime('%H:%M:%S')}] "
              f"Processed: {human_size(processed_bytes)} | "
              f"Remaining: {human_size(remaining_bytes)} | "
              f"ETA: {eta_str} | "
              f"Current file ({human_size(src_size)}): {src.resolve()}")

        # --- Diagnose sweep: encode this file under every candidate preset ---
        # Read the source once (cold) to measure disk speed and warm the cache, then
        # run each preset's encode against that warm cache so their speeds compare
        # cleanly without disk noise. This path fully handles the file and continues;
        # the normal single-pass logic below is skipped. (--diagnose is a no-op under
        # --dry-run, so dry-run is off here.)
        if args.diagnose and not args.dry_run:
            read_seconds, read_bytes = measure_read_speed(src)
            logging.info(f"[DIAGNOSE] Disk read: {src.name}: "
                         f"{human_size(read_bytes)} in "
                         f"{human_duration(read_seconds, include_seconds=True)} = "
                         f"{format_throughput(read_bytes, read_seconds)}")
            file_had_encode = False
            for preset in presets_to_test:
                dst = diagnose_tagged_dst(src, input_base, args.output_folder, preset,
                                          args.encoding, args.crf)
                if dst.exists() and not args.force:
                    print(f"[{time.strftime('%H:%M:%S')}] Output file exists: {dst}")
                    logging.info(f"SKIPPED (output file already exists): {dst}")
                    skipped_existing += 1
                    continue
                stats = diagnose_encode_one(src, dst, preset, read_seconds, read_bytes, args)
                if stats is not None:
                    file_had_encode = True
                    encoded_count += 1
                    total_orig += stats["orig_bytes"]
                    total_new += stats["new_bytes"]
                    total_duration_seconds += stats["video_seconds"]
                    ps = preset_stats[preset]
                    ps["enc_seconds"] += stats["enc_seconds"]
                    ps["enc_bytes"] += stats["orig_bytes"]
                    ps["new_bytes"] += stats["new_bytes"]
                    ps["video_seconds"] += stats["video_seconds"]
                    ps["files"] += 1
                    # For a single-preset diagnose (no sweep), also feed the aggregate
                    # accumulators that drive the bottleneck-verdict summary. For a
                    # sweep, that block is replaced by the comparison table, so summing
                    # encode time across presets there would be meaningless.
                    if not diag_sweep:
                        diag_encode_seconds += stats["enc_seconds"]
                        diag_encode_bytes += stats["orig_bytes"]
                        diag_encode_new_bytes += stats["new_bytes"]
                        diag_video_seconds += stats["video_seconds"]
            # Read stats are per file (shared across presets), so accumulate once.
            if file_had_encode:
                diag_read_seconds += read_seconds
                diag_read_bytes += read_bytes
            processed_bytes += src_size
            continue
        # --- End diagnose sweep ---

        call_start = time.monotonic()
        try:
            result = process_file(src, dst, args.crf, args.duration, args.min_size_mb,
                                   args.dry_run, args.encoding,
                                   args.normalize_audio, args.loudnorm_target, args.downscale,
                                   args.strip_no_english_audio, args.preset,
                                   processed_bytes, args.deinterlace, args.audio_codec)
        except ConversionError as e:
            # Timeouts are called out separately in the log (they point at a stuck
            # process rather than a bad encode) but are handled identically: skip the
            # file and keep going. The circuit breaker below is what stops a run when
            # the fault is systemic rather than per-file.
            failed_path = e.file.resolve()
            if isinstance(e, ConversionTimeoutError):
                logging.error(f"TIMEOUT: {failed_path}\n{e.reason}")
            else:
                logging.error(f"CONVERSION FAILED: {failed_path}\n{e.reason}")
            failed_files.append(failed_path)
            processed_bytes += src_size
            consecutive_failures += 1
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                _SCRIPT_STOP_NOTE = (f"ABORTED after {consecutive_failures} consecutive "
                                     f"failures")
                logging.error(f"Stopping: {consecutive_failures} file(s) in a row failed, "
                              f"which points at a systemic problem (source drive offline, "
                              f"ffmpeg missing) rather than individual bad files. "
                              f"Remaining files were not processed.")
                print(f"\n{COLOR_ERROR}Stopping after {consecutive_failures} consecutive "
                      f"failures — see the log.{COLOR_RESET}")
                break
            continue
        call_elapsed = time.monotonic() - call_start

        processed_bytes += src_size
        consecutive_failures = 0  # this file got through; the run is healthy again

        if result is not None:
            (orig_size, new_size, video_duration, action, downscaled, grew_larger,
     retried, interlace_hinted) = result
            total_orig += orig_size
            total_new += new_size
            if video_duration is not None:
                total_duration_seconds += video_duration
            if action == "encoded":
                encoded_count += 1
            else:
                copied_count += 1
            if downscaled:
                downscaled_count += 1
            if grew_larger:
                grew_larger_count += 1
            if retried:
                retried_count += 1
            if interlace_hinted:
                interlace_hinted_count += 1

            # Informational only, and log-file only (logging.info isn't echoed to the
            # console). Skipped on dry runs, where no output file exists to measure.
            if not args.dry_run:
                subtitle_measurement = measure_subtitle_bytes(dst)
                if subtitle_measurement is not None:
                    sub_bytes, sub_tracks = subtitle_measurement
                    subtitle_bytes_total += sub_bytes
                    subtitle_tracks_total += sub_tracks
                    if sub_tracks == 0:
                        # Stated plainly rather than as "0.0B across 0 track(s)". This
                        # line also covers copied files (already HEVC / below the size
                        # threshold), which never reach process_file's
                        # "SUBTITLE TRACKS FOUND" check.
                        logging.info(f"SUBTITLE SIZE: no subtitle tracks in output: {dst}")
                    else:
                        pct = f"{sub_bytes / new_size * 100:.2f}%" if new_size else "--"
                        logging.info(f"SUBTITLE SIZE: {human_size(sub_bytes)} across "
                                     f"{sub_tracks} track(s), {pct} of the "
                                     f"{human_size(new_size)} output: {dst}")

    if skipped_existing:
        logging.info(f"{skipped_existing} file(s) skipped because the output file already existed.")

    if limit_reached:
        remaining_unprocessed = total_files - index
        if remaining_unprocessed:
            logging.info(f"{remaining_unprocessed} file(s) left unprocessed due to --limit.")

    logging.info(f"Batch conversion complete [{mode}].")

    if failed_files:
        logging.info(f"{len(failed_files)} file(s) failed to convert:")
        for f in failed_files:
            logging.info(f"  FAILED: {f}")
        print(f"\n{len(failed_files)} file(s) failed to convert:")
        for f in failed_files:
            print(f"  - {f}")

    breakdown = (f"Encoded: {encoded_count} | Copied: {copied_count} | "
                 f"Skipped (existing): {skipped_existing} | Failed: {len(failed_files)}")
    logging.info(breakdown)
    print(f"\n{breakdown}")

    downscale_line = f"Downscaled from >1080p: {downscaled_count} file(s)"
    logging.info(downscale_line)
    print(downscale_line)

    grew_larger_line = f"Larger after encoding: {grew_larger_count} file(s)"
    logging.info(grew_larger_line)
    print(grew_larger_line)

    retried_line = f"Needed a retry after a transient encode failure: {retried_count} file(s)"
    logging.info(retried_line)
    print(retried_line)

    # Only printed when there's something to report: on a progressive library this
    # would otherwise be a permanent "0 file(s)" line that trains the eye to skip it.
    if interlace_hinted_count:
        interlace_line = (f"Possibly interlaced (encoded as-is, --deinterlace was off): "
                          f"{interlace_hinted_count} file(s) — see POSSIBLE INTERLACING "
                          f"in the log, and re-check them with --deinterlace auto")
        logging.info(interlace_line)
        print(f"{COLOR_WARNING}{interlace_line}{COLOR_RESET}")

    if args.diagnose and diag_sweep and diag_read_seconds > 0:
        any_encoded = any(s["files"] > 0 for s in preset_stats.values())
        if any_encoded:
            table_header = [
                "",
                "=== Preset comparison (--diagnose sweep) ===",
                f"Disk read (shared, measured once per file): "
                f"{format_throughput(diag_read_bytes, diag_read_seconds)}",
                f"Encoders run against a warm cache, so 'enc MB/s' reflects the encoder, "
                f"not the disk. 'proj. full job' extrapolates each preset's speed over "
                f"the whole input tree ({human_size(total_size_bytes)}, {total_files} files).",
                "",
            ]
            table_lines = build_preset_comparison_table(preset_stats, tuple(presets_to_test),
                                                        total_size_bytes)
            note = ("(A faster preset near the top of the table finishes sooner; a "
                    "slower one usually compresses a little better at the same CRF. "
                    "Pick the fastest preset whose 'smaller' % and picture you're "
                    "happy with. Files larger than free RAM won't fully cache, "
                    "slightly understating encode speed.)")
            for line in table_header + table_lines + ["", note]:
                logging.info(line)
                print(line)

    if args.diagnose and not diag_sweep and diag_encode_bytes > 0 and diag_read_seconds > 0:
        read_mbps = diag_read_bytes / (1024 * 1024) / diag_read_seconds
        enc_mbps = diag_encode_bytes / (1024 * 1024) / diag_encode_seconds
        bottleneck_mbps = min(read_mbps, enc_mbps)
        if read_mbps < enc_mbps * 0.9:
            verdict = ("DISK I/O is your bottleneck. The encoder can consume data "
                       "faster than the disk delivers it, so a faster --preset will "
                       "NOT speed up the overall job. Staging files to an SSD first, "
                       "or encoding several files in parallel so one file's encode "
                       "overlaps another's disk read, is what would help.")
        elif enc_mbps < read_mbps * 0.9:
            verdict = ("ENCODING is your bottleneck. The disk can deliver data faster "
                       "than the encoder consumes it, so a faster --preset (e.g. "
                       "medium instead of veryslow) should directly speed up the job.")
        else:
            verdict = ("Disk and encoder are roughly balanced. A faster --preset may "
                       "help somewhat, but you'll hit the disk ceiling soon after.")
        diag_summary = [
            "",
            "=== Throughput diagnosis ===",
            f"Avg disk read speed:  {format_throughput(diag_read_bytes, diag_read_seconds)}",
        ]

        # Encode speed line, with the bitrate-independent realtime factor appended when
        # we have video durations to compute it from.
        encode_speed_line = (f"Avg encode speed:     "
                             f"{format_throughput(diag_encode_bytes, diag_encode_seconds)} "
                             f"(source consumed, cache-warmed)")
        if diag_video_seconds > 0 and diag_encode_seconds > 0:
            encode_speed_line += f"  |  {diag_video_seconds / diag_encode_seconds:.1f}x realtime"
        diag_summary.append(encode_speed_line)

        # Compression achieved across the encoded sample.
        if diag_encode_bytes > 0:
            pct_smaller = (1 - diag_encode_new_bytes / diag_encode_bytes) * 100
            ratio = diag_encode_bytes / diag_encode_new_bytes if diag_encode_new_bytes > 0 else 0
            diag_summary.append(
                f"Avg compression:      {human_size(diag_encode_bytes)} -> "
                f"{human_size(diag_encode_new_bytes)}, {pct_smaller:.0f}% smaller "
                f"({ratio:.2f}x) across {encoded_count} encoded file(s)")

        diag_summary.append(
            f"Effective ceiling:    ~{bottleneck_mbps * 86400 / 1024:.0f} GB/day "
            f"(the slower of the two)")

        # Full-job projection over everything discovered in the input tree. total_size_bytes
        # is the sum of ALL matched source files (the whole tree unless narrowed by
        # --include/--exclude), so pointing --diagnose at the real media root with --limit
        # to cap the sample gives a projection over the true remaining corpus.
        if bottleneck_mbps > 0 and total_size_bytes > 0:
            proj_seconds = total_size_bytes / (bottleneck_mbps * 1024 * 1024)
            proj_line = (f"Projected full job:   {human_size(total_size_bytes)} across "
                         f"{total_files} file(s) -> ~"
                         f"{human_duration(proj_seconds, include_seconds=True)} "
                         f"at this rate")
            diag_summary.append(proj_line)
            if diag_encode_bytes > 0 and diag_encode_new_bytes > 0:
                proj_ratio = diag_encode_new_bytes / diag_encode_bytes
                proj_out = total_size_bytes * proj_ratio
                proj_saved = total_size_bytes - proj_out
                diag_summary.append(
                    f"Projected size:       ~{human_size(proj_out)} output, "
                    f"~{human_size(proj_saved)} saved (if the sample's compression holds)")

        diag_summary.append(verdict)
        diag_summary.append(
            "(Encode speed is measured with the OS cache warmed by the diagnostic "
            "read, so it reflects the encoder more than the disk. Files larger than "
            "free RAM won't fully cache, which narrows the gap. Projection assumes the "
            "whole tree encodes like the sampled files — already-HEVC files that get "
            "copied instead will finish faster and shrink less.)")

        for line in diag_summary:
            logging.info(line)
            print(line)

    script_runtime = time.monotonic() - batch_start
    script_runtime_line = f"Script runtime: {human_duration(script_runtime, include_seconds=True)}"

    if args.dry_run:
        logging.info(script_runtime_line)
        print(script_runtime_line)
        print(f"\n[DRY RUN] No files were modified. Log written to: {log_path}")
    else:
        reduction_bytes = total_orig - total_new
        reduction_pct = (abs(reduction_bytes) / total_orig * 100) if total_orig else 0
        if reduction_bytes >= 0:
            summary = (f"Total reduction: {human_size(reduction_bytes)}, "
                        f"{reduction_pct:.1f}% smaller "
                        f"({human_size(total_orig)} -> {human_size(total_new)})")
        else:
            # Net growth can happen when downscaled files (exempt from the
            # discard-on-growth check) grow enough to outweigh savings elsewhere.
            summary = (f"Total size INCREASED by {human_size(-reduction_bytes)}, "
                        f"{reduction_pct:.1f}% larger "
                        f"({human_size(total_orig)} -> {human_size(total_new)})")
        logging.info(summary)
        print(f"\n{summary}")

        # Log file only, deliberately not printed: this is reference data rather than
        # something to watch a long run by.
        sub_pct = (f"{subtitle_bytes_total / total_new * 100:.2f}% of output"
                   if total_new else "--")
        logging.info(f"Total subtitle data: {human_size(subtitle_bytes_total)} across "
                     f"{subtitle_tracks_total} track(s) in {encoded_count + copied_count} "
                     f"file(s), {sub_pct}")

        runtime_summary = f"Total video running time: {human_duration(total_duration_seconds)}"
        logging.info(runtime_summary)
        print(runtime_summary)
        logging.info(script_runtime_line)
        print(script_runtime_line)
        print(f"Log written to: {log_path}")

    final_line = f"Finished with {len(failed_files)} error(s)."
    logging.info(final_line)
    print(final_line)


if __name__ == "__main__":
    # The end banner is written from here rather than from main() so it covers every
    # way out: normal return, sys.exit (the CRF-comparison path exits explicitly),
    # Ctrl-C, and crashes. Each exception is re-raised unchanged afterwards, so exit
    # codes and console behaviour are exactly what they would be without this wrapper.
    try:
        main()
    except SystemExit as exc:
        code = exc.code
        if code is None or code == 0:
            log_script_end(_SCRIPT_STOP_NOTE or "completed")
        else:
            log_script_end(f"exited with code {code}")
        raise
    except KeyboardInterrupt:
        log_script_end("INTERRUPTED by user (Ctrl-C) - the file in progress was not "
                       "finished")
        raise
    except BaseException as exc:
        log_script_end(f"CRASHED ({type(exc).__name__}: {exc})",
                       traceback.format_exc())
        raise
    else:
        log_script_end(_SCRIPT_STOP_NOTE or "completed")
