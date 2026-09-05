#!/usr/bin/env python3
"""
Batch-convert .mp4 and .mkv files in a folder (recursively) to H.265/HEVC using ffmpeg,
running software encoding. Files already encoded in H.265 are copied as-is.

Usage:
    python convert_videos.py [-i input_folder] [-o output_folder] [--crf 22] [--duration -1]

If -i/-o are omitted, both default to the current folder. If the output folder
is the same as the input folder, converted files replace the originals in place
after a successful conversion.

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
from pathlib import Path


def _enable_windows_ansi_support():
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
        kernel32 = ctypes.windll.kernel32
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
_SUPPORTS_COLOR = sys.stderr.isatty()
COLOR_WARNING = "\033[93m" if _SUPPORTS_COLOR else ""  # yellow
COLOR_ERROR = "\033[91m" if _SUPPORTS_COLOR else ""    # red
COLOR_RESET = "\033[0m" if _SUPPORTS_COLOR else ""


class ConversionError(Exception):
    """Raised when ffprobe or ffmpeg fails for a given file."""
    def __init__(self, file: Path, reason: str):
        self.file = file
        self.reason = reason
        super().__init__(f"{file}: {reason}")


class ConversionTimeoutError(ConversionError):
    """Raised when an ffprobe subprocess exceeds its allotted timeout. Treated as fatal
    for the whole run (unlike other ConversionErrors, which just skip the file and
    continue): a metadata probe that stalls this long points at a genuinely stuck
    process or unreadable file, so it's worth stopping to investigate rather than
    silently skipping ahead. Note that ffmpeg encode/decode passes deliberately run
    without a timeout — see measure_loudness and process_file."""
    pass


# Minimum timeout for any single ffprobe call, regardless of file size, so very small
# files still get a sane floor rather than a near-zero allowance.
TIMEOUT_FLOOR_SECONDS = 30 * 60  # 30 minutes

# Additional timeout allowance per GB of source file size, covering slow reads on
# large files over network mounts or spinning disks. Deliberately generous: ffprobe
# only reads container metadata, so taking longer than this points at a stuck process
# rather than merely slow work.
TIMEOUT_SECONDS_PER_GB = 15 * 60  # 15 minutes per GB


def compute_timeout_seconds(src_size_bytes: int) -> float:
    """A generous timeout for a single ffprobe metadata read over a file this size, so
    a hung probe on a corrupt or unusual file doesn't stall the batch indefinitely.
    Not applied to ffmpeg encode or decode passes, which are unbounded — capping those
    by wall clock produced false positives on slow-but-healthy encodes."""
    size_gb = src_size_bytes / (1024 ** 3)
    return TIMEOUT_FLOOR_SECONDS + size_gb * TIMEOUT_SECONDS_PER_GB


def build_parser():
    parser = argparse.ArgumentParser(
        description="Recursively re-encode .mp4/.mkv files to H.265 (hardware via Intel "
                    "Quick Sync by default; use --encoding=software for libx265). Files "
                    "already in H.265 are copied through unchanged, not re-encoded. "
                    "Optional flags add 1080p downscaling (-d), non-English audio "
                    "stripping (-e), and EBU R128 loudness normalization "
                    "(--normalize-audio). See --compare-crf to test-encode files "
                    "at multiple CRF values side by side."
    )
    parser.add_argument("-i", "--input", dest="input_folder", type=Path, default=Path("."),
                         help="Folder to scan recursively for .mp4/.mkv files. Default: current folder")
    parser.add_argument("-o", "--output", dest="output_folder", type=Path, default=None,
                         help="Folder to write converted/copied files to. Default: current folder. "
                              "If this is the same as the input folder, converted files replace "
                              "the originals in place. Required when --duration is set (test encodes "
                              "must not overwrite your source files).")
    parser.add_argument("-q", "--crf", type=int, default=22,
                         help="x265 CRF value (lower = higher quality/larger file). Default: 22")
    parser.add_argument("-t", "--duration", type=float, default=-1,
                         help="Encode only the first N seconds of each file. "
                              "Default: -1 (encode the full file)")
    parser.add_argument("--dry-run", action="store_true",
                         help="List what would be done for each file without actually "
                              "encoding or copying anything.")
    parser.add_argument("--min-size-mb", type=float, default=50,
                         help="Files smaller than this size (in MB) are skipped and just "
                              "copied to the output folder as-is. Default: 50")
    parser.add_argument("--encoding", choices=["hardware", "software"], default="hardware",
                         help="Encoding mode. 'software' uses libx265 (CPU). 'hardware' uses "
                              "Intel Quick Sync (hevc_qsv). Default: hardware")
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
    parser.add_argument("--compare-crf", type=str, default=None, metavar="CRF1,CRF2,...",
                         help="Comparison mode: test-encode every file at each given CRF "
                              "(e.g. 18,22,28,35), printing a size/time table per file plus "
                              "an aggregate table across files. Uses --duration for a quick "
                              "clip test if set, otherwise encodes full files. Requires "
                              "-o/--output different from -i/--input. Writes "
                              "<name>_crf<value>_<hardware|software><ext> plus an unmodified "
                              "<name>_original<ext> for side-by-side comparison.")
    return parser


def parse_args():
    return build_parser().parse_args()


class ColorConsoleFormatter(logging.Formatter):
    """Colors WARNING messages yellow and ERROR/CRITICAL messages red when printed
    to the console. COLOR_WARNING/COLOR_ERROR/COLOR_RESET are empty strings when
    stderr isn't a real terminal, so redirected/piped output stays plain text."""
    def format(self, record):
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

    # Full detail (INFO and up) goes to the log file, plain text.
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
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


def probe_media(path: Path, timeout_seconds: float) -> dict:
    """Probe a media file with a single ffprobe call, returning:
      {
        "video": {"codec_name": str, "width": int, "height": int, "duration": float|None,
                   "stream_index": int},  # index i in ffmpeg's 0:v:i, for explicit mapping
        "audio_languages": [lang_or_None, ...],   # index i == ffmpeg's 0:a:i
        "subtitle_tracks": [(lang_or_None, codec_name), ...],  # index i == 0:s:i
      }
    The video stream chosen is the first one that isn't embedded cover art (an
    attached_pic, or an image-y codec like mjpeg/png/bmp/gif), so a thumbnail
    that precedes the real video stream isn't mistaken for it.
    Raises ConversionError if ffprobe fails or no real video stream is found, or
    ConversionTimeoutError if it doesn't finish within timeout_seconds."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "stream=codec_name,codec_type,width,height,disposition:stream_tags=language:format=duration",
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

    video_candidates = []  # every non-cover-art video stream seen, in order, with its 0:v:i index
    fallback_video_info = None  # first video stream at all, in case every one looks like cover art
    audio_languages = []
    subtitle_tracks = []
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


def probe_duration(path: Path, timeout_seconds: float) -> float:
    """Return the duration (seconds) of a media file via a lightweight ffprobe call, or
    None if duration could not be determined. Raises ConversionError if ffprobe itself
    fails to read the file (a strong signal of a corrupt/incomplete output), or
    ConversionTimeoutError if it doesn't finish within timeout_seconds."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]
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


def measure_loudness(src: Path, duration: float, loudnorm_target: float,
                      audio_stream_index: int) -> dict:
    """Runs loudnorm's analysis pass (decode + filter, no output file written) against
    the given audio stream to measure its actual loudness stats, for feeding into a
    second, exact pass of EBU R128 normalization. Returns the parsed stats dict
    (keys include input_i, input_tp, input_lra, input_thresh, target_offset).
    Raises ConversionError if ffmpeg fails or the stats block can't be found/parsed,
    so callers can fall back to one-pass normalization for this file. No timeout is
    applied: a full decode pass on a large file can legitimately run a long time, and
    a wall-clock cap produced false positives on slow-but-healthy work."""
    cmd = ["ffmpeg", "-i", str(src)]
    if duration != -1:
        cmd += ["-t", str(duration)]
    cmd += [
        "-map", f"0:a:{audio_stream_index}",
        "-af", f"loudnorm=I={loudnorm_target}:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-",
    ]
    logging.info(f"Command: {format_cmd_for_log(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
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


def human_size(num_bytes: int) -> str:
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


def format_cmd_for_log(cmd: list) -> str:
    """Join a subprocess argv list into a loggable command string, wrapping file/
    folder path arguments — the input path after -i, and a real output path at
    the end — in double quotes, so paths containing spaces are unambiguous when
    read back from the log (and the line can be copy-pasted into a shell as-is).
    Flags and their non-path values are left unquoted. The trailing "-" ffmpeg
    uses for a null/pipe output (as in the loudness measurement pass) is left
    unquoted too, since it isn't actually a path."""
    parts = []
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
                      loudnorm_target: float = -16, audio_stream_indices: list = None,
                      subtitle_stream_indices: list = None, video_stream_index: int = 0,
                      measured_loudness: dict = None) -> list:
    cmd = ["ffmpeg", "-y", "-i", str(src)]

    if duration != -1:
        cmd += ["-t", str(duration)]

    audio_stream_indices = audio_stream_indices or []
    subtitle_stream_indices = subtitle_stream_indices or []

    # Explicit stream mapping disables ffmpeg's automatic "best stream" selection,
    # so the video stream must always be mapped too. video_stream_index picks out
    # the real video track (as identified by probe_media) rather than assuming
    # it's the first video stream, since embedded cover art is also a video
    # stream and can precede the real one.
    cmd += ["-map", f"0:v:{video_stream_index}"]
    for idx in audio_stream_indices:
        cmd += ["-map", f"0:a:{idx}"]
    for idx in subtitle_stream_indices:
        cmd += ["-map", f"0:s:{idx}"]

    if needs_downscale:
        # Scale down so neither dimension exceeds 1080p, preserving aspect ratio.
        # force_original_aspect_ratio=decrease only shrinks, never upscales.
        # force_divisible_by=2 rounds the calculated dimension to an even number,
        # since x265/x264 require even width & height for 4:2:0 chroma subsampling.
        cmd += ["-vf", "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2"]

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
        # handles that fine, but libopus doesn't support quality-scale mode at all and
        # refuses to open, aborting the whole encode with no output written.
        cmd += ["-pix_fmt", "p010le", "-c:v", "hevc_qsv", "-global_quality:v", str(crf),
                "-preset", "veryslow", "-low_power", "0",
                "-look_ahead", "1", "-look_ahead_depth", "40"]
    else:
        # yuv420p10le: encode in 10-bit. Even for 8-bit sources, x265's finer
        # quantization steps in 10-bit mode noticeably improve compression
        # efficiency at a given CRF, at negligible compatibility cost on modern
        # players/decoders.
        cmd += ["-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-preset", "slow", "-crf", str(crf)]

    cmd += ["-c:a", "libopus", "-ac", "2", "-b:a", "128k"]

    if normalize_audio:
        # Scoped to output audio stream 0 (-filter:a:0) rather than the unscoped -af,
        # which would apply this exact filter description — including the fixed
        # measured_* gain values below, which are only valid for the primary track —
        # identically to every kept audio stream. Only the primary/first mapped
        # audio track is normalized; any other kept tracks are still re-encoded to
        # Opus above, just without a loudness filter applied.
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

    if subtitle_stream_indices:
        # Passthrough: subtitle tracks are copied as-is, not re-encoded. Since the
        # output container always matches the input's (same file extension), the
        # original subtitle codec (subrip, ass, PGS, etc.) remains valid.
        cmd += ["-c:s", "copy"]

    cmd += [str(dst)]
    return cmd


def process_file(src: Path, dst: Path, crf: int, duration: float, min_size_mb: float,
                  same_location: bool, dry_run: bool = False, encoding: str = "software",
                  normalize_audio: bool = True, loudnorm_target: float = -16,
                  downscale: bool = False, strip_non_english_audio: bool = False):
    """Returns (original_size, new_size, video_duration_seconds, action, downscaled,
    grew_larger) on success or dry-run preview, or None only when the caller already
    decided to skip the file entirely before calling this (not used internally here;
    reserved for callers). action is 'encoded' or 'copied'. downscaled is True if the
    file was scaled down from >1080p. video_duration_seconds may be None if duration
    could not be determined at all. In dry-run mode, new_size is a placeholder equal to
    original_size (no actual encode happens, so the real output size is unknown) — safe
    because dry-run never prints the "Total reduction" summary that would otherwise
    misuse it; it's only used for the Encoded/Copied/Failed breakdown and --limit
    accounting, both of which need original_size and action/downscaled, not a real
    new_size. grew_larger is True only when a real encode came out bigger than the
    source and was discarded in favor of copying the original through instead
    (action='copied', new_size==original_size in that case) — always False in dry-run
    mode, since no real encode happened to compare against, and always False when
    duration != -1, since a partial clip's size isn't comparable to the full source's.
    This discard-and-fall-back behavior, and grew_larger, only apply to the normal
    batch path, not run_crf_comparison's test encodes, where seeing every CRF's actual
    size (including growth) is the point. Raises ConversionError if ffprobe or ffmpeg fails on a file
    that must be processed (small below-threshold files are copied regardless of a
    duration-probe failure, since they were never going to be encoded)."""
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
            raise  # fatal: propagate up so the run stops and can be investigated
        except ConversionError as e:
            logging.warning(f"Could not determine duration for below-threshold file "
                             f"(copying anyway): {e.reason}: {src}")
            small_file_duration = None

        if same_location:
            logging.info(f"KEPT (below {min_size_mb}MB minimum, already in place, "
                         f"{human_size(src_size)}): {src}")
            return (src_size, src_size, small_file_duration, "copied", False, False)
        if dry_run:
            logging.info(f"[DRY RUN] WOULD COPY (below {min_size_mb}MB minimum, "
                         f"{human_size(src_size)}): {src} -> {dst}")
            return (src_size, src_size, small_file_duration, "copied", False, False)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logging.info(f"COPIED (below {min_size_mb}MB minimum, "
                     f"{human_size(src_size)}): {src} -> {dst}")
        return (src_size, src_size, small_file_duration, "copied", False, False)

    media = probe_media(src, timeout_seconds)
    video_info = media["video"]
    codec = video_info.get("codec_name", "")
    width = video_info.get("width", 0)
    height = video_info.get("height", 0)
    video_duration = video_info.get("duration")

    if codec == "hevc":
        if same_location:
            logging.info(f"KEPT (already H.265, already in place): {src}")
            return (src_size, src_size, video_duration, "copied", False, False)
        if dry_run:
            logging.info(f"[DRY RUN] WOULD COPY (already H.265): {src} -> {dst}")
            return (src_size, src_size, video_duration, "copied", False, False)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logging.info(f"COPIED (already H.265): {src} -> {dst}")
        return (src_size, src_size, video_duration, "copied", False, False)

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

    # Subtitle/CC tracks: always carry through every English-tagged track; drop the
    # rest (including untagged, since it can't be confirmed English).
    keep_subtitle_indices = [i for i, (lang, codec) in enumerate(subtitle_tracks)
                              if lang in ("eng", "en")]
    if subtitle_tracks:
        kept_str = (", ".join(f"{i}:{subtitle_tracks[i][0] or 'und'}" for i in keep_subtitle_indices)
                    if keep_subtitle_indices else "none")
        dropped = [i for i in range(len(subtitle_tracks)) if i not in keep_subtitle_indices]
        dropped_str = (", ".join(f"{i}:{subtitle_tracks[i][0] or 'und'}" for i in dropped)
                       if dropped else "none")
        logging.info(f"SUBTITLE: keeping English track(s) [{kept_str}]; "
                     f"discarding [{dropped_str}]: {src}")

    log_action = "[DRY RUN] WOULD ENCODE" if dry_run else "ENCODING"
    logging.info(f"{log_action}: {src} -> {dst} (codec={codec}, {width}x{height}, "
                 f"downscale={'yes' if needs_downscale else 'no'}, crf={crf}, "
                 f"encoding={encoding}, "
                 f"normalize_audio={'yes (' + str(loudnorm_target) + ' LUFS)' if normalize_audio else 'no'}, "
                 f"duration={'full' if duration == -1 else f'{duration}s'})")

    if dry_run:
        # Dry runs never invoke ffmpeg, so there's no measured loudness stats to show
        # here; the preview command falls back to the one-pass filter shape. The real
        # encode (below) runs an actual two-pass measurement when normalize_audio is on.
        cmd = build_ffmpeg_cmd(src, dst, crf, duration, needs_downscale, encoding,
                                normalize_audio, loudnorm_target, keep_audio_indices,
                                keep_subtitle_indices, video_info["stream_index"])
        logging.info(f"[DRY RUN] Command: {format_cmd_for_log(cmd)}")
        if normalize_audio and keep_audio_indices:
            logging.info(f"[DRY RUN] Note: audio will be normalized in two passes "
                         f"(a measurement pass, then the exact-gain encode shown above "
                         f"reflects the one-pass shape only): {src}")
        # new_size is unknown without actually encoding, so src_size is reported as a
        # placeholder (no reduction assumed). This is safe because the "Total
        # reduction" summary is never printed in dry-run mode, only the
        # Encoded/Copied/Failed breakdown and --limit accounting, which need orig_size
        # and action/downscaled, not a real new_size.
        return (src_size, src_size, video_duration, "encoded", needs_downscale, False)

    # When replacing in place, ffmpeg can't read and write the same path at once,
    # so encode to a temp file alongside it, then swap it in on success. The real
    # extension must stay last (".converting.tmp.mp4", not "....mp4.converting.tmp"),
    # since ffmpeg picks its output container by the final suffix and can't infer one
    # from ".tmp".
    if same_location:
        encode_target = dst.parent / f".{dst.stem}.converting.tmp{dst.suffix}"
    else:
        encode_target = dst

    encode_target.parent.mkdir(parents=True, exist_ok=True)

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

    cmd = build_ffmpeg_cmd(src, encode_target, crf, duration, needs_downscale, encoding,
                            normalize_audio, loudnorm_target, keep_audio_indices,
                            keep_subtitle_indices, video_info["stream_index"],
                            measured_loudness)
    logging.info(f"Command: {format_cmd_for_log(cmd)}")
    encode_start = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        if encode_target.exists():
            encode_target.unlink(missing_ok=True)
        raise ConversionError(src, f"ffmpeg exited with code {result.returncode}: "
                                    f"{result.stderr[-2000:]}")

    encode_elapsed = time.monotonic() - encode_start

    # Post-encode validation: confirm the output's duration roughly matches the
    # source's before trusting it (and, for in-place mode, before it ever overwrites
    # the original). Skipped for test/partial encodes (--duration), since those are
    # intentionally shorter than the source.
    if duration == -1 and video_duration is not None:
        output_duration = probe_duration(encode_target, timeout_seconds)
        if output_duration is None:
            if encode_target.exists():
                encode_target.unlink(missing_ok=True)
            raise ConversionError(src, "post-encode validation failed: could not "
                                        "determine output duration")
        tolerance = max(2.0, 0.02 * video_duration)
        if abs(output_duration - video_duration) > tolerance:
            if encode_target.exists():
                encode_target.unlink(missing_ok=True)
            raise ConversionError(src, f"post-encode validation failed: source duration "
                                        f"{video_duration:.1f}s vs output duration "
                                        f"{output_duration:.1f}s (tolerance {tolerance:.1f}s)")
        logging.info(f"VALIDATED: output duration {output_duration:.1f}s matches source "
                     f"{video_duration:.1f}s: {src}")

    # Check the candidate output's size before committing it, so a converted file that
    # ended up larger than the source is never kept — growing storage instead of
    # shrinking it defeats the point of this script. This check happens before the
    # in-place swap below, so for same_location the original at dst/src is never
    # touched if the encode is discarded. Skipped when --duration is set: the candidate
    # is only the first N seconds, so comparing its size against the full source's is
    # meaningless (a short clip of a big file always "shrinks"; a clip of a tiny source
    # could spuriously "grow" and get replaced by a full-length copy of the original,
    # which isn't the test output the user asked for). --compare-crf deliberately
    # doesn't apply this either: seeing every CRF's real size, including ones that grew,
    # is the whole point of that comparison.
    candidate_size = encode_target.stat().st_size
    if duration == -1 and candidate_size > src_size:
        growth_pct = (candidate_size / src_size - 1) * 100 if src_size else 0
        if same_location:
            encode_target.unlink(missing_ok=True)
            logging.warning(f"DISCARDED (encode grew {human_size(src_size)} -> "
                            f"{human_size(candidate_size)}, +{growth_pct:.1f}%); "
                            f"original kept unchanged: {src}")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)  # overwrites the too-large candidate at dst
            logging.warning(f"DISCARDED (encode grew {human_size(src_size)} -> "
                            f"{human_size(candidate_size)}, +{growth_pct:.1f}%); "
                            f"copied original instead: {src} -> {dst}")
        return (src_size, src_size, video_duration, "copied", False, True)

    if same_location:
        os.replace(encode_target, dst)  # dst == src here; atomic swap-in

    # Preserve the source file's modification/access time on the newly encoded output.
    os.utime(dst, (src_stat.st_atime, src_stat.st_mtime))

    orig_size = src_size
    new_size = dst.stat().st_size if dst.exists() else 0
    saved_pct = (1 - new_size / orig_size) * 100 if orig_size else 0

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
    return (orig_size, new_size, video_duration, "encoded", needs_downscale, False)


def run_crf_comparison(src: Path, output_folder: Path, crf_values: list, duration: float,
                        encoding: str, downscale: bool) -> tuple:
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
    filenames include src.stem and the encoding type, so multiple files' test encodes
    (and a hardware vs. software re-run of the same file) coexist in the same output
    folder without colliding.
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
    header = f"CRF comparison for: {src.name}  ({clip_label}, {encoding} encoding)"
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
        logging.info(f"Original comparison copy already exists, skipping: {original_dst}")
        original_copied = True
    else:
        try:
            if duration == -1:
                shutil.copy2(src, original_dst)
            else:
                copy_cmd = ["ffmpeg", "-y", "-i", str(src), "-t", str(duration),
                            "-c", "copy", str(original_dst)]
                logging.info(f"Command: {format_cmd_for_log(copy_cmd)}")
                copy_result = subprocess.run(copy_cmd, capture_output=True, text=True)
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
    keep_subtitle_indices = list(range(len(media["subtitle_tracks"])))

    rows = []  # (crf, size_bytes_or_None, elapsed_seconds, error_or_None, skipped)
    for crf in crf_values:
        dst = output_folder / f"{src.stem}_crf{crf}_{encoding}{src.suffix}"

        if dst.exists():
            logging.info(f"CRF {crf}: output already exists, skipping encode: {dst}")
            rows.append((crf, dst.stat().st_size, 0.0, None, True))
            continue

        cmd = build_ffmpeg_cmd(src, dst, crf, duration, needs_downscale, encoding,
                                False, -16, keep_audio_indices, keep_subtitle_indices,
                                video_info["stream_index"], None)
        logging.info(f"Command: {format_cmd_for_log(cmd)}")

        start = time.monotonic()
        result = subprocess.run(cmd, capture_output=True, text=True)
        elapsed = time.monotonic() - start

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


def print_crf_aggregate_summary(aggregate: dict, crf_values: list, files_compared: int) -> None:
    """Prints/logs one summary table folding every file's CRF comparison together, so
    the best CRF for a whole batch of varied content is easy to read off in one place
    rather than eyeballing each file's individual table. aggregate maps crf -> {orig,
    new, time, ok, failed, timed_ok} as accumulated by main(); % reduction is computed
    over every success (ok), while avg time is computed only over timed_ok (freshly
    encoded files, excluding ones that reused an existing output) so a skipped-existing
    file's elapsed=0 doesn't drag the average down. failed/timed-out attempts are
    reported as a count, not folded into the size/time totals, since they contributed
    no size or a meaningless partial time."""
    header = f"\nAggregate CRF comparison across {files_compared} file(s):"
    print(header)
    logging.info(header.strip())

    lines = [f"{'CRF':>5}  {'Files OK':>8}  {'Total Size':>11}  "
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


def main():
    args = parse_args()

    if not args.input_folder.is_dir():
        print(f"Input folder does not exist: {args.input_folder}", file=sys.stderr)
        sys.exit(1)

    video_extensions = ("*.mp4", "*.MP4", "*.mkv", "*.MKV")
    mp4_files = sorted(set().union(*(args.input_folder.rglob(pat) for pat in video_extensions)))

    if not mp4_files:
        print(f"No .mp4 or .mkv files found in: {args.input_folder}\n", file=sys.stderr)
        build_parser().print_help()
        sys.exit(1)

    if args.duration != -1 and args.output_folder is None:
        print("Error: -o/--output is required when --duration is set. "
              "Test encodes must be written to a separate folder so your source "
              "files are never overwritten with a partial encode.", file=sys.stderr)
        sys.exit(1)

    crf_values = None
    if args.compare_crf is not None:
        if args.output_folder is None:
            print("Error: -o/--output is required when --compare-crf is set.",
                  file=sys.stderr)
            sys.exit(1)
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

    if args.output_folder is None:
        args.output_folder = Path(".")

    input_resolved = args.input_folder.resolve()
    output_resolved = args.output_folder.resolve()
    same_location = input_resolved == output_resolved

    if args.duration != -1 and same_location:
        print("Error: output folder must be different from the input folder when "
              "--duration is set. Test encodes must not overwrite your source files.",
              file=sys.stderr)
        sys.exit(1)

    if crf_values is not None and same_location:
        print("Error: output folder must be different from the input folder when "
              "--compare-crf is set.", file=sys.stderr)
        sys.exit(1)

    # Comparison runs get an encoder-tagged log, so a hardware and a software run over
    # the same output folder produce separate logs rather than interleaving in one.
    log_suffix = f"_{args.encoding}" if crf_values is not None else ""
    log_path = setup_logging(args.output_folder, log_suffix)

    if crf_values is not None:
        logging.info(f"CRF comparison mode: {len(mp4_files)} file(s) found in "
                     f"{input_resolved}, values: {crf_values}")
        # Per-CRF totals across every file, so a batch of files can be compared as a
        # whole rather than only reading each file's own table individually. Only
        # successful encodes (err is None) contribute to orig/new/ok; a CRF value that
        # fails or times out on a file still gets counted in "failed" for that CRF.
        # "timed_ok" tracks only freshly-encoded successes (not reused existing output),
        # so a skipped-existing file's elapsed=0 doesn't drag down the avg-time column.
        aggregate = {crf: {"orig": 0, "new": 0, "time": 0.0, "ok": 0, "failed": 0, "timed_ok": 0}
                     for crf in crf_values}
        files_compared = 0
        total_files = len(mp4_files)
        for index, src in enumerate(mp4_files, start=1):
            progress = f"[{index}/{total_files}] Processing: {src.name}"
            print(f"\n{progress}")
            logging.info(progress)
            try:
                src_size, rows = run_crf_comparison(src, args.output_folder, crf_values,
                                                     args.duration, args.encoding,
                                                     args.downscale)
            except ConversionTimeoutError as e:
                logging.error(f"TIMEOUT: {e.file.resolve()}\n{e.reason}")
                logging.error("Stopping the run so this timeout can be investigated. "
                              "Remaining files were not processed.")
                sys.exit(1)
            except ConversionError as e:
                logging.error(f"CRF comparison skipped for {e.file.resolve()}: {e.reason}")
                continue

            files_compared += 1
            for crf, size, elapsed, err, skipped in rows:
                if err is None:
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

        sys.exit(0)

    mode = "DRY RUN" if args.dry_run else "LIVE"
    logging.info(f"Starting batch conversion [{mode}]. Input: {input_resolved} "
                 f"Output: {output_resolved} "
                 f"({'in-place' if same_location else 'separate output'}) CRF: {args.crf} "
                 f"Encoding: {args.encoding} "
                 f"Normalize audio: {'yes (' + str(args.loudnorm_target) + ' LUFS)' if args.normalize_audio else 'no'} "
                 f"Duration limit: {'none' if args.duration == -1 else f'{args.duration}s'} "
                 f"Min size: {args.min_size_mb}MB "
                 f"Data limit: {'none' if args.limit == -1 else f'{args.limit}GB'} "
                 f"Downscale to 1080p: {'yes' if args.downscale else 'no'} "
                 f"Strip non-English audio: {'yes' if args.strip_no_english_audio else 'no'}")

    logging.info(f"Found {len(mp4_files)} .mp4/.mkv file(s) to process.")

    total_orig = 0
    total_new = 0
    total_duration_seconds = 0.0
    failed_files = []
    skipped_existing = 0
    encoded_count = 0
    copied_count = 0
    downscaled_count = 0
    grew_larger_count = 0
    limit_bytes = float("inf") if args.limit == -1 else args.limit * 1024 ** 3
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

        print(f"Processed: {human_size(processed_bytes)} | "
              f"Remaining: {human_size(remaining_bytes)} | "
              f"ETA: {eta_str} | "
              f"Current file ({human_size(src_size)}): {src.name}")

        if same_location:
            dst = src
        else:
            rel_path = src.relative_to(args.input_folder)
            dst = args.output_folder / rel_path

        if not same_location and dst.exists() and not args.force:
            prefix = "[DRY RUN] WOULD SKIP" if args.dry_run else "SKIPPED"
            logging.info(f"{prefix} (output file already exists): {dst}")
            skipped_existing += 1
            processed_bytes += src_size
            continue

        try:
            result = process_file(src, dst, args.crf, args.duration, args.min_size_mb,
                                   same_location, args.dry_run, args.encoding,
                                   args.normalize_audio, args.loudnorm_target, args.downscale,
                                   args.strip_no_english_audio)
        except ConversionTimeoutError as e:
            failed_path = e.file.resolve()
            logging.error(f"TIMEOUT: {failed_path}\n{e.reason}")
            logging.error("Stopping the run so this timeout can be investigated. "
                          "Remaining files were not processed.")
            sys.exit(1)
        except ConversionError as e:
            failed_path = e.file.resolve()
            logging.error(f"CONVERSION FAILED: {failed_path}\n{e.reason}")
            failed_files.append(failed_path)
            processed_bytes += src_size
            continue

        processed_bytes += src_size

        if result is not None:
            orig_size, new_size, video_duration, action, downscaled, grew_larger = result
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

    grew_larger_line = f"Larger after encoding (discarded, original kept): {grew_larger_count} file(s)"
    logging.info(grew_larger_line)
    print(grew_larger_line)

    script_runtime = time.monotonic() - batch_start
    script_runtime_line = f"Script runtime: {human_duration(script_runtime, include_seconds=True)}"

    if args.dry_run:
        logging.info(script_runtime_line)
        print(script_runtime_line)
        print(f"\n[DRY RUN] No files were modified. Log written to: {log_path}")
    else:
        reduction_bytes = total_orig - total_new
        reduction_pct = (reduction_bytes / total_orig * 100) if total_orig else 0
        summary = (f"Total reduction: {human_size(reduction_bytes)}, "
                    f"{reduction_pct:.1f}% smaller "
                    f"({human_size(total_orig)} -> {human_size(total_new)})")
        logging.info(summary)
        print(f"\n{summary}")
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
    main()
