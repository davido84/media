#!/usr/bin/env python3
"""
iso_to_mkv.py
=============

Batch-convert .iso files to .mkv using makemkvcon (the MakeMKV command line
tool), with automatic title filtering, original-language audio preservation,
English subtitle preservation, and basic Playlist Obfuscation (a.k.a.
"ScreenPass" / ARccOS-style UOPs, seen on discs like John Wick) detection.

-----------------------------------------------------------------------------
IMPORTANT ASSUMPTIONS / CAVEATS (please read before relying on this in prod)
-----------------------------------------------------------------------------
1. This script parses makemkvcon's "robot mode" (-r) output. The attribute
   IDs used below (duration=9, disk size in bytes=11, name=2, stream
   type=1, language code=3, language name=4, info/comment=30) match values
   confirmed either in MakeMKV's own documentation or in real -r output
   posted on the MakeMKV forums. If a future MakeMKV release changes
   these, re-check with:
       makemkvcon -r info iso:/path/to/disc.iso | less
   and adjust the constants near the top of this file if needed.

2. Playlist-obfuscation ("ScreenPass" / UOPs) handling uses TWO signals,
   checked in this order:

     a) MakeMKV's own Java-based detection. If the Java Runtime Environment
        (JRE) is installed and correctly located by MakeMKV, MakeMKV will
        run the disc's BD-Java code, work out which playlist is really the
        main feature, and mark that title's info/comment field (attribute
        30) with the literal text "(FPL_MainFeature)". This is confirmed,
        real MakeMKV behavior (see MakeMKV forum threads on FPL_MainFeature)
        and is a far more reliable signal than any duration-based guess,
        so it is checked first and trusted when unambiguous.
        NOTE: this only works if the JRE is installed and makemkvcon can
        find it; the disc itself doesn't need any special handling from
        this script for that to work, other than not overriding title
        selection. Also note (per MakeMKV's own user community) that Java
        detection is NOT 100% reliable - it sometimes fails to run, and
        rarely flags the wrong title - so treat it as strong evidence, not
        absolute certainty, and spot check important discs.
        The script also watches for MakeMKV's own confirmed message "This
        disc requires Java runtime (JRE), but none was found" (see
        https://www.makemkv.com/bdjava/) and logs an explicit WARNING
        distinguishing "this disc needed Java and none was found" from
        "this disc just never engaged Java" (jre_required_missing vs.
        jre_engaged=False) - the former is a real, fixable problem
        (install a JRE, or point MakeMKV at one via app_Java in
        ~/.MakeMKV/settings.conf), the latter is simply a disc that never
        needed BD-Java in the first place.
        When this signal is what identified the title (not this script's
        own duration-based fallback below), that title's output file is
        named "main_title.mkv" instead of MakeMKV's default name, so other
        tools working on the output folder afterward (organize_media.py,
        a HandBrake batch script, etc.) can trust the filename rather than
        re-deriving which title was the main feature themselves.

     b) A duration-clustering fallback, used only when no unambiguous
        FPL_MainFeature marker is found. This looks for a very large
        number of titles that all share (almost) the same duration - the
        classic signature of ScreenPass/UOPs discs presenting hundreds of
        decoy playlists. The threshold defaults to 100 titles sharing a
        duration (--obfuscation-threshold); a low threshold like 5 is NOT
        used here on purpose, since it's common for perfectly ordinary
        discs (TV box sets, multi-angle features, bonus loops) to have a
        handful of titles at the same length with no obfuscation involved
        at all - only a "hundreds of titles" pattern is a reliable tell.
        When this fallback fires, and after filtering by --min-length
        there is exactly ONE title left that could be the main feature,
        that title is extracted. If more than one plausible candidate
        remains, we cannot safely guess, so a WARNING is logged and the
        disc is skipped entirely (source file left untouched).

   In both cases, makemkvcon is NEVER invoked with "all" - only explicit
   title numbers are ever passed.

   Both signals above are Blu-ray specific (BD-Java doesn't exist on DVD,
   and DVD protection schemes don't produce hundreds of decoy titles), so
   this script first classifies each ISO as DVD or Blu-ray by FILE SIZE
   and skips this entire section for anything classified as DVD - see
   point 2a-DVD below.

2a-DVD. DVD vs Blu-ray classification is a SIZE HEURISTIC, not a real
   filesystem inspection (e.g. checking for a BDMV vs VIDEO_TS folder
   inside the ISO). DVD-9, the largest standard DVD format, holds at most
   8.5 GB (decimal). Any real Blu-ray rip is essentially always well
   above that (BD25 = 25 GB, BD50 = 50 GB, UHD BD66/100 larger still), so
   "ISO size > threshold" is a cheap and normally very reliable stand-in
   for "this is a Blu-ray". Tune with --dvd-max-size-gb (default 8.5,
   decimal GB i.e. size * 10**9 bytes, matching how disc capacities are
   marketed) if your library has unusual outliers, or force it with
   --disc-type={dvd,bluray} to skip the heuristic entirely for a run.
   Known edge case: a manually re-authored/trimmed "backup" ISO that's
   smaller than a full Blu-ray but still a Blu-ray filesystem would be
   misclassified as DVD by this heuristic; use --disc-type=bluray for
   batches like that.

3. Every audio and subtitle track on each extracted title is kept as-is -
   this script does NOT attempt any language filtering or track
   selection. That used to be attempted in two different ways (a bogus
   trailing CLI argument to makemkvcon, then a two-pass mkvmerge remux),
   but both were dropped: makemkvcon has no CLI mechanism to select
   tracks at all (confirmed by a real failure and corroborated by
   MakeMKV's own forums, where this has been a known, unaddressed
   limitation since at least 2011 - "Can't extract specific audio files.
   Can't specify subtitles."), and the mkvmerge-based workaround added
   real cost (a second remux pass, doubled disk/time, an extra
   dependency, and a subtle trust issue where mkvmerge's own exit code
   doesn't reliably indicate whether a selection actually succeeded) for
   something that's simpler to handle in a later encoding pass instead
   (e.g. HandBrakeCLI, which already supports audio/subtitle track
   selection as part of encoding). So: this script's job stops at
   "extract each wanted title losslessly, with everything on it" -
   language/track curation is intentionally left to whatever processes
   the .mkv files next.

5. Discovery, extraction, min-length filtering, and logging are otherwise
   identical for DVD and Blu-ray - only the obfuscation-detection section
   (point 2/2a-DVD) actually branches by disc type.

6. "Play All" concatenation title detection (common on TV-show DVDs, where
   one title is every episode stitched back-to-back so a DVD player can
   play the whole disc as one stream): after the candidate title list is
   otherwise finalized, the single LONGEST candidate is tested as the
   play-all hypothesis (a real play-all title is, by construction, longer
   than any individual episode, so only one hypothesis needs testing).
   The OTHER candidates are first clustered by similarity to their own
   median duration (--playall-cluster-tolerance-pct, default 30%), so a
   bonus featurette or trailer that also clears --min-length doesn't
   throw off the comparison - only the similarly-sized "episode" cluster
   is summed. If that sum is within tolerance of the longest title's
   duration (--playall-tolerance-sec, default 30s, plus 2s/episode extra
   slack for rounding), the longest title is treated as Play All and
   discarded; everything else - the episodes AND any bonus content
   outside the cluster - is kept for extraction as normal. This is always
   logged when it fires; nothing is discarded silently. Disable with
   --detect-playall=False if a disc's real structure ever fights with
   this.

Test this script with --dry-run against your library before turning it
loose, and consider trying it on a copy of a known ScreenPass disc (e.g.
John Wick) to confirm the obfuscation heuristic behaves the way you want.

7. Safety/workflow additions on top of the above:
     - Before deleting a source ISO, the script verifies a real (non-tiny)
       .mkv file actually appeared/changed in the output directory for
       each extracted title - a 0-exit-code from makemkvcon is treated as
       necessary but not sufficient. See MIN_OUTPUT_FILE_BYTES.
     - A pre-flight check confirms makemkvcon can be found/executed
       before any files are touched. Beyond that, if
       --max-consecutive-failures (default 3) ISOs in a row fail at the
       initial title-info scan, the whole run stops early rather than
       grinding through the rest of the batch with what's almost always
       a systemic problem (bad path, expired registration key,
       permissions) rather than bad media.
     - Free space on the output volume is checked before each title
       extraction (using MakeMKV's own reported title size plus
       --free-space-margin-pct headroom), so a nearly-full output drive
       fails fast on one title instead of partway through a multi-hour
       extraction.
     - Resume support: if an ISO's natural output folder already
       contains at least as many real .mkv files as the number of
       titles this run would extract, the ISO is skipped entirely
       (logged, counted separately in the summary) - this is the
       default behavior. This is whole-ISO granularity only - an
       interrupted multi-title disc is safely redone in full rather
       than partially resumed. Force a redo with -f/--force.
     - Title extraction streams makemkvcon's progress output live
       (parsing PRGT/PRGV robot-mode lines) instead of going silent for
       the whole extraction; only shown when stdout is a real terminal.
     - Ctrl+C during a run terminates the in-flight makemkvcon child
       process cleanly, then still prints the summary-so-far and closes
       the log, rather than leaving an orphaned process or a truncated
       log file.

8. --include=REGEX / --exclude=REGEX (mutually exclusive - argparse
   rejects passing both) filter the discovered ISO list before any
   processing starts. The regex is matched with re.search (no anchoring
   required) against each ISO's full resolved path, not just the
   filename, so you can filter by a folder name too (e.g. a show or
   season directory) as well as by filename. --include keeps only
   matching files; --exclude keeps only non-matching files. A summary of
   how many files matched is logged once, before the batch begins.

Note: Jellyfin/Plex-friendly output naming (renaming the main feature to
"<Title> (<Year>).mkv" and extras to "extra.<n>.mkv") used to live here,
but was deliberately split out into a separate script, organize_media.py,
which operates on already-extracted .mkv files instead. Naming/organizing
is a media-library problem, not a disc-extraction problem, and the split
means it can be re-run safely any number of times with zero risk to
source ISOs, and works on .mkv files from any source, not just this
script. See organize_media.py's own docstring for details.
-----------------------------------------------------------------------------
"""

import argparse
import csv
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_LOG = "./convert.log"

# MakeMKV robot-mode attribute IDs (see module docstring, point 1)
ATTR_NAME = 2
ATTR_DURATION = 9
ATTR_DISKSIZE_BYTES = 11
ATTR_INFO = 30  # title "info/comment" text; carries "(FPL_MainFeature)" when JRE identifies it

# Exact marker MakeMKV writes into a title's info text when its BD-Java
# analysis (requires JRE) has identified that title as the main feature.
# Matched with parentheses so we don't also match variants like
# "FPL_MainFeature_UR" (seen on some discs with alternate cuts), which
# need manual disambiguation rather than being silently treated as equal.
FPL_MAIN_FEATURE_RE = re.compile(r"\(FPL_MainFeature\)")
FPL_SUBSTRING = "FPL_MainFeature"

# Exact, confirmed MakeMKV message (see e.g. makemkv.com/bdjava/ and its own
# forums) printed when a disc actually needs BD-Java (fake-playlist
# protection, BD+ handshake, or Soft-KCD) but no JRE could be found. This is
# a much stronger, more specific signal than the mere absence of the
# "Using Java runtime" success line, which also doesn't appear on discs
# that never needed Java in the first place.
JRE_MISSING_MARKER = "This disc requires Java runtime (JRE), but none was found"

DVD_MAX_SIZE_GB_DEFAULT = 8.5  # decimal GB (10**9 bytes), matching how DVD-9 capacity is marketed
DISC_TYPE_DVD = "DVD"
DISC_TYPE_BLURAY = "BLURAY"

# Sanity floor for "does this look like a real output file", used both to
# verify a title actually got extracted before deleting the source (safety
# enhancement 1) and to decide whether an existing output directory counts
# as "already converted" for resume support (enhancement 4). This is a
# floor, not a quality check - any real title clearing --min-length will
# produce something far larger than this.
MIN_OUTPUT_FILE_BYTES = 1_000_000  # 1 MB


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def str2bool(value: str) -> bool:
    if isinstance(value, bool):
        return value
    v = value.strip().lower()
    if v in ("true", "t", "yes", "y", "1"):
        return True
    if v in ("false", "f", "no", "n", "0"):
        return False
    raise argparse.ArgumentTypeError(f"Expected a boolean value (True/False), got: {value!r}")


def human_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PB"


def format_duration(seconds: float) -> str:
    seconds = int(round(max(seconds, 0)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_mkv_duration(value: str) -> float:
    """Parse MakeMKV's 'H:MM:SS' duration string into seconds."""
    try:
        parts = [int(p) for p in value.split(":")]
    except ValueError:
        return 0.0
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return float(h * 3600 + m * 60 + s)


def csv_fields(payload: str) -> List[str]:
    """Parse a single CSV-style line (MakeMKV robot output is CSV after the
    leading TAG: prefix, with quoted values)."""
    try:
        return next(csv.reader([payload]))
    except StopIteration:
        return []


def classify_disc(iso_path: Path, max_dvd_bytes: float, override: Optional[str]) -> str:
    """Heuristically classify an ISO as DVD or Blu-ray. See module
    docstring point 2a-DVD for rationale and known limitations."""
    if override:
        return DISC_TYPE_DVD if override == "dvd" else DISC_TYPE_BLURAY
    size = iso_path.stat().st_size
    return DISC_TYPE_BLURAY if size > max_dvd_bytes else DISC_TYPE_DVD


def preflight_check_makemkvcon(makemkvcon_bin: str) -> Optional[str]:
    """Confirm the makemkvcon binary can actually be found/executed before
    processing any files, so a bad --makemkvcon path or missing install
    fails fast with one clear message instead of every ISO in the batch
    failing individually with the same root cause (safety enhancement 2)."""
    if shutil.which(makemkvcon_bin) is None:
        return f"makemkvcon executable not found or not executable: {makemkvcon_bin!r}"
    return None


def snapshot_output_dir(out_dir: Path) -> Dict[str, int]:
    """Map of filename -> size for every file currently in out_dir."""
    if not out_dir.is_dir():
        return {}
    return {p.name: p.stat().st_size for p in out_dir.iterdir() if p.is_file()}


def existing_output_mkvs(out_dir: Path) -> List[Path]:
    """.mkv files in out_dir that look like real output, not zero-byte
    leftovers from an interrupted run."""
    if not out_dir.is_dir():
        return []
    return [
        p for p in out_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".mkv" and p.stat().st_size >= MIN_OUTPUT_FILE_BYTES
    ]


def check_free_space(out_dir: Path, output_root: Path, required_bytes: int, margin_pct: float) -> Optional[str]:
    """Return None if there's enough free space on the output volume for
    an extraction of about required_bytes (plus a safety margin), else an
    error message. Checks whichever of out_dir/output_root already exists,
    since out_dir may not have been created yet (safety enhancement 3)."""
    check_path = out_dir if out_dir.exists() else output_root
    try:
        free = shutil.disk_usage(check_path).free
    except OSError as e:
        return f"Could not determine free space on {check_path}: {e}"
    needed = required_bytes * (1 + margin_pct / 100.0)
    if free < needed:
        return (
            f"Insufficient free space on output volume: need ~{human_bytes(needed)} "
            f"(incl. {margin_pct:g}% margin), only {human_bytes(free)} free"
        )
    return None


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

class DualLogger:
    """Writes to console (ISO name only, no path) and to a log file
    (full ISO path), per the requested behavior."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.log_path, "a", encoding="utf-8")
        self._raw(f"\n==== Run started {datetime.now().isoformat(timespec='seconds')} ====")

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _raw(self, line: str) -> None:
        self._fh.write(line + "\n")
        self._fh.flush()

    def append_raw_to_file(self, line: str) -> None:
        self._raw(line)

    def _log(self, level: str, message: str, iso_path: Optional[Path]) -> None:
        ts = self._timestamp()
        console_target = f" {iso_path.name}:" if iso_path is not None else ""
        file_target = f" {iso_path}:" if iso_path is not None else ""
        print(f"{ts} [{level}]{console_target} {message}")
        self._raw(f"{ts} [{level}]{file_target} {message}")

    def info(self, message: str, iso_path: Optional[Path] = None) -> None:
        self._log("INFO", message, iso_path)

    def warning(self, message: str, iso_path: Optional[Path] = None) -> None:
        self._log("WARNING", message, iso_path)

    def error(self, message: str, iso_path: Optional[Path] = None) -> None:
        self._log("ERROR", message, iso_path)

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------
# makemkvcon interaction
# --------------------------------------------------------------------------

def run_cmd(cmd: List[str]) -> Tuple[int, str]:
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return proc.returncode, proc.stdout or ""
    except FileNotFoundError as e:
        return 127, f"Could not execute command {cmd!r}: {e}"


def run_cmd_with_progress(cmd: List[str], on_progress=None) -> Tuple[int, str]:
    """Like run_cmd, but streams output line-by-line so PRGT/PRGV progress
    lines can be reported live via on_progress(percent, label) while a
    long title extraction is running (workflow enhancement 5), instead of
    going silent until the whole title finishes. Still returns the full
    captured output for post-hoc error diagnostics, same as run_cmd.

    On Ctrl+C, explicitly terminates (then kills, if needed) the child
    process before re-raising, so an interrupt doesn't leave an orphaned
    makemkvcon running in the background (workflow enhancement 6)."""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
    except FileNotFoundError as e:
        return 127, f"Could not execute command {cmd!r}: {e}"

    lines: List[str] = []
    current_label: Optional[str] = None
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            lines.append(line)
            if on_progress is None:
                continue
            if line.startswith("PRGT:"):
                fields = csv_fields(line[len("PRGT:"):])
                if len(fields) >= 3:
                    current_label = fields[2]
            elif line.startswith("PRGV:"):
                fields = csv_fields(line[len("PRGV:"):])
                if len(fields) >= 3:
                    try:
                        _current, total, maxv = int(fields[0]), int(fields[1]), int(fields[2])
                        if maxv > 0:
                            pct = min(100.0, max(0.0, (total / maxv) * 100.0))
                            on_progress(pct, current_label)
                    except ValueError:
                        pass
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    finally:
        if proc.poll() is None:
            proc.wait()

    return proc.returncode, "\n".join(lines)


def make_progress_printer(iso_name: str, title_id: int):
    """Returns an on_progress callback that prints a live-updating
    progress line to the console (never to the log file - that would just
    be noise). Only prints when stdout is a real terminal, since a \\r
    updating line doesn't make sense when output is redirected to a file."""
    if not sys.stdout.isatty():
        return None
    state = {"last_pct": -1}

    def _cb(pct: float, label: Optional[str]) -> None:
        pct_int = int(pct)
        if pct_int == state["last_pct"]:
            return
        state["last_pct"] = pct_int
        text = f"    {iso_name} title {title_id}: {pct_int:3d}%"
        if label:
            text += f" ({label})"
        sys.stdout.write("\r" + text.ljust(90))
        sys.stdout.flush()

    return _cb


@dataclass
class Title:
    title_id: int
    duration_sec: float = 0.0
    size_bytes: int = 0
    name: Optional[str] = None
    info_text: Optional[str] = None  # attribute 30; may contain "(FPL_MainFeature)"


def get_disc_titles(makemkvcon_bin: str, iso_path: Path) -> Tuple[int, str, Dict[int, Title], bool, bool]:
    """Run `makemkvcon info` on the ISO and parse the title table.

    Returns (returncode, raw_output, titles, jre_engaged, jre_required_missing).
    jre_engaged is True if makemkvcon reported it launched the Java runtime
    for this disc (i.e. it attempted BD-Java based main-feature detection).
    jre_required_missing is True if this disc specifically needed Java (for
    fake-playlist protection, a BD+ handshake, or Soft-KCD) and MakeMKV
    could not find a JRE to use - a much stronger signal than jre_engaged
    simply being False, which is also true for every disc that never
    needed Java at all."""
    cmd = [makemkvcon_bin, "-r", "--cache=1", "info", f"iso:{iso_path}"]
    rc, output = run_cmd(cmd)
    titles: Dict[int, Title] = {}
    jre_engaged = "Using Java runtime" in output
    jre_required_missing = JRE_MISSING_MARKER in output

    for line in output.splitlines():
        line = line.strip()
        if line.startswith("TINFO:"):
            fields = csv_fields(line[len("TINFO:"):])
            if len(fields) < 4:
                continue
            try:
                tid, code = int(fields[0]), int(fields[1])
            except ValueError:
                continue
            value = fields[3]
            t = titles.setdefault(tid, Title(title_id=tid))
            if code == ATTR_DURATION:
                t.duration_sec = parse_mkv_duration(value)
            elif code == ATTR_DISKSIZE_BYTES:
                try:
                    t.size_bytes = int(value)
                except ValueError:
                    pass
            elif code == ATTR_NAME:
                t.name = value
            elif code == ATTR_INFO:
                t.info_text = value

    return rc, output, titles, jre_engaged, jre_required_missing


def looks_like_warning(output: str) -> Optional[str]:
    """Best-effort scan of makemkvcon output for non-fatal warning lines,
    even when the overall command succeeded."""
    for line in output.splitlines():
        low = line.lower()
        if "warning" in low or "some titles" in low:
            return line.strip()
    return None


# --------------------------------------------------------------------------
# Title / track selection logic
# --------------------------------------------------------------------------

def detect_obfuscation(titles: Dict[int, Title], threshold: int) -> Tuple[bool, float, int]:
    """Bucket ALL titles by duration; if any bucket is huge, suspect
    playlist obfuscation. Returns (suspected, duration_of_bucket, bucket_size)."""
    buckets: Dict[int, List[int]] = defaultdict(list)
    for tid, t in titles.items():
        buckets[int(round(t.duration_sec))].append(tid)
    if not buckets:
        return False, 0.0, 0
    biggest_duration, biggest_ids = max(buckets.items(), key=lambda kv: len(kv[1]))
    return len(biggest_ids) >= threshold, float(biggest_duration), len(biggest_ids)


MIN_CANDIDATES_FOR_PLAYALL_DETECTION = 3  # need the concat title plus >= 2 episodes


def detect_playall_title(
    candidates: List[int],
    titles: Dict[int, Title],
    tolerance_sec: float,
    cluster_tolerance_pct: float,
) -> Optional[Tuple[int, List[int]]]:
    """Detect a "Play All" concatenation title: common on TV-show DVDs,
    where one title is just all the individual episodes stitched together
    back-to-back so a DVD player can play the whole disc as one stream.

    Only the single LONGEST candidate is ever tested as the play-all
    candidate - a real play-all title is, by construction, longer than
    any individual episode, so it's the only title that could plausibly
    be one.

    Rather than summing every OTHER candidate's duration (which would be
    thrown off by a bonus featurette or trailer that also clears
    --min-length sitting alongside the episodes - a very normal thing to
    find on a real TV season disc), the other candidates are first
    clustered by similarity to their own median duration
    (--playall-cluster-tolerance-pct). Only that similarly-sized cluster
    (the presumed episodes) is summed and compared to the play-all
    candidate; anything outside the cluster (bonus content) is left alone
    and stays a normal extraction candidate either way.

    Returns (playall_title_id, sorted_episode_cluster_ids) or None.
    """
    if len(candidates) < MIN_CANDIDATES_FOR_PLAYALL_DETECTION:
        return None

    longest_tid = max(candidates, key=lambda tid: titles[tid].duration_sec)
    longest_duration = titles[longest_tid].duration_sec
    others = [tid for tid in candidates if tid != longest_tid]
    if len(others) < 2:
        return None

    other_durations = sorted(titles[tid].duration_sec for tid in others)
    median = other_durations[len(other_durations) // 2]
    if median <= 0:
        return None

    cluster = [
        tid for tid in others
        if abs(titles[tid].duration_sec - median) <= (cluster_tolerance_pct / 100.0) * median
    ]
    if len(cluster) < 2:
        return None

    cluster_sum = sum(titles[tid].duration_sec for tid in cluster)
    # Allow a little extra slack per episode for frame/GOP rounding, on top
    # of the flat --playall-tolerance-sec floor.
    effective_tolerance = max(tolerance_sec, 2.0 * len(cluster))

    if abs(longest_duration - cluster_sum) <= effective_tolerance:
        return longest_tid, sorted(cluster)
    return None


def unique_output_dir(output_root: Path, stem: str, used: set) -> Path:
    name = stem
    n = 2
    while name in used:
        name = f"{stem}_{n}"
        n += 1
    used.add(name)
    return output_root / name


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------

@dataclass
class Stats:
    conversions_success: int = 0
    conversions_error: int = 0
    warnings: int = 0
    isos_converted: int = 0
    bytes_converted: int = 0
    already_converted_skipped: int = 0


@dataclass
class ProcessResult:
    """Outcome of process_iso, richer than a plain bool so main() can
    drive the resume/limit/circuit-breaker logic around it."""
    converted: bool = False           # fully converted this run (counts toward --limit, eligible for deletion)
    skipped_already_done: bool = False  # resume: output already existed, nothing was done
    info_scan_failed: bool = False    # couldn't even read title info - used for the circuit breaker


# --------------------------------------------------------------------------
# Per-ISO processing
# --------------------------------------------------------------------------

def process_iso(
    iso_path: Path,
    output_root: Path,
    args: argparse.Namespace,
    logger: DualLogger,
    stats: Stats,
    used_output_names: set,
) -> ProcessResult:
    """Returns a ProcessResult describing what happened, so main() can
    drive --limit accounting, source deletion, and the info-scan
    circuit breaker."""

    min_length_sec = args.min_length * 60.0

    # --- Resume support (workflow enhancement 4) ---
    # Checked against the disc's natural (non-disambiguated) output path,
    # since that's what a previous run would have used. Only a whole-ISO
    # check is done - if a multi-title disc was interrupted partway
    # through, this deliberately does NOT try to resume just the missing
    # titles (predicting makemkvcon's own output filenames well enough to
    # do that safely is fragile); it will just fully redo that one ISO,
    # which is the safe default over a false "looks done" skip.
    natural_out_dir = output_root / iso_path.stem
    if not args.force:
        pre_existing_mkvs = existing_output_mkvs(natural_out_dir)
    else:
        pre_existing_mkvs = []

    disc_type_override = None if args.disc_type == "auto" else args.disc_type
    disc_type = classify_disc(iso_path, args.dvd_max_size_gb * 1_000_000_000, disc_type_override)
    logger.info(f"Classified as {disc_type} ({human_bytes(iso_path.stat().st_size)})", iso_path)

    rc, output, titles, jre_engaged, jre_required_missing = get_disc_titles(args.makemkvcon, iso_path)
    if rc != 0 or not titles:
        logger.error(f"Failed to read title information (exit code {rc})", iso_path)
        logger.append_raw_to_file(output)
        stats.conversions_error += 1
        return ProcessResult(info_scan_failed=True)

    # Set below only when MakeMKV's own (FPL_MainFeature) marker identifies
    # a title - not when this script's own duration-based fallback picks
    # one. That title's output gets named "main_title.mkv" (see the
    # per-title loop) so other tools can trust the filename rather than
    # re-deriving which title was the main feature.
    fpl_identified_main_tid: Optional[int] = None

    if disc_type == DISC_TYPE_DVD:
        # Blu-ray-specific detection (BD-Java / FPL_MainFeature, and the
        # duration-clustering fallback that exists to catch Blu-ray-style
        # ScreenPass decoys) doesn't apply to DVDs - see docstring point
        # 2a-DVD. Just take every title that passes --min-length.
        candidates = [tid for tid, t in titles.items() if t.duration_sec >= min_length_sec]

    else:
        if jre_engaged:
            logger.info("MakeMKV engaged the Java runtime for BD-Java main-feature analysis", iso_path)
        elif jre_required_missing:
            logger.warning(
                "This disc requires a Java runtime (JRE) for BD-Java processing (fake-playlist "
                "protection, a BD+ handshake, or Soft-KCD), but MakeMKV could not find one - "
                "install a JRE or set app_Java in MakeMKV's settings.conf. See "
                "https://www.makemkv.com/bdjava/",
                iso_path,
            )
            stats.warnings += 1

        suspected, dup_duration, dup_count = detect_obfuscation(titles, args.obfuscation_threshold)

        # --- Signal 1 (preferred): MakeMKV's own JRE/BD-Java main-feature marker ---
        fpl_exact = [tid for tid, t in titles.items() if t.info_text and FPL_MAIN_FEATURE_RE.search(t.info_text)]
        fpl_variant = [
            tid for tid, t in titles.items()
            if t.info_text and FPL_SUBSTRING in t.info_text and tid not in fpl_exact
        ]

        if fpl_variant:
            logger.warning(
                f"Found {len(fpl_variant)} title(s) with an FPL_MainFeature-style variant marker "
                f"(e.g. FPL_MainFeature_UR) - these need manual review, not auto-selection",
                iso_path,
            )

        if fpl_exact:
            if len(fpl_exact) > 1:
                logger.warning(
                    f"MakeMKV flagged {len(fpl_exact)} titles as (FPL_MainFeature) - ambiguous, skipping disc",
                    iso_path,
                )
                stats.warnings += 1
                return ProcessResult()
            main_tid = fpl_exact[0]
            if titles[main_tid].duration_sec < min_length_sec:
                logger.warning(
                    f"Title {main_tid} was flagged (FPL_MainFeature) but is shorter than "
                    f"--min-length ({format_duration(titles[main_tid].duration_sec)}) - skipping disc",
                    iso_path,
                )
                stats.warnings += 1
                return ProcessResult()
            logger.info(
                f"MakeMKV's Java-based analysis identified title {main_tid} as (FPL_MainFeature)",
                iso_path,
            )
            candidates = [main_tid]
            fpl_identified_main_tid = main_tid

        else:
            # --- Signal 2 (fallback): duration-clustering heuristic ---
            candidates = [tid for tid, t in titles.items() if t.duration_sec >= min_length_sec]

            if suspected:
                if jre_required_missing:
                    jre_note = " - JRE was required but not found, see warning above"
                elif not jre_engaged:
                    jre_note = " - JRE was not engaged"
                else:
                    jre_note = ", and Java did not resolve it"
                logger.warning(
                    f"Suspected Playlist Obfuscation: {dup_count} titles share a duration of "
                    f"~{format_duration(dup_duration)} (no (FPL_MainFeature) marker found{jre_note})",
                    iso_path,
                )
                stats.warnings += 1
                if len(candidates) != 1:
                    logger.warning(
                        f"Cannot reliably determine the correct main title "
                        f"({len(candidates)} candidates >= min length) - skipping disc",
                        iso_path,
                    )
                    stats.warnings += 1
                    return ProcessResult()
                # Exactly one unambiguous candidate remains - safe to proceed.

    if not candidates:
        logger.warning(f"No titles >= {args.min_length:g} minutes found - skipping disc", iso_path)
        stats.warnings += 1
        return ProcessResult()

    if args.detect_playall:
        result = detect_playall_title(
            candidates, titles, args.playall_tolerance_sec, args.playall_cluster_tolerance_pct
        )
        if result is not None:
            playall_tid, episode_ids = result
            logger.info(
                f"Identified title {playall_tid} (duration "
                f"{format_duration(titles[playall_tid].duration_sec)}) as a 'Play All' concatenation "
                f"of {len(episode_ids)} episode title(s) {episode_ids} (summed duration "
                f"{format_duration(sum(titles[t].duration_sec for t in episode_ids))}) - "
                f"discarding it and keeping the individual episodes",
                iso_path,
            )
            candidates = [tid for tid in candidates if tid != playall_tid]

    if not candidates:
        logger.warning("Only a 'Play All' concatenation title was found - skipping disc", iso_path)
        stats.warnings += 1
        return ProcessResult()

    # Resume check happens here, once we know how many titles we'd
    # actually want to extract - if a previous complete run already left
    # at least that many real .mkv files sitting in the natural output
    # dir, treat this ISO as already done rather than re-extracting it.
    if pre_existing_mkvs and len(pre_existing_mkvs) >= len(candidates):
        logger.info(
            f"Found {len(pre_existing_mkvs)} existing .mkv file(s) in {natural_out_dir} "
            f"(expected {len(candidates)}) - looks already converted, skipping. "
            f"Use --force to redo.",
            iso_path,
        )
        stats.already_converted_skipped += 1
        return ProcessResult(skipped_already_done=True)

    out_dir = unique_output_dir(output_root, iso_path.stem, used_output_names)
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    all_ok = True
    for tid in sorted(candidates):
        title = titles[tid]
        logger.info(
            f"Extracting title {tid} (duration {format_duration(title.duration_sec)}) -> {out_dir}",
            iso_path,
        )

        # Every audio/subtitle track on the title is extracted as-is - see
        # docstring point 3 for why this script doesn't attempt track
        # filtering (makemkvcon has no CLI mechanism for it at all, and a
        # prior mkvmerge-based workaround was deliberately removed in
        # favor of leaving track curation to a later encoding pass).
        cmd = [args.makemkvcon, "-r", "--cache=1", "mkv", f"iso:{iso_path}", str(tid), str(out_dir)]

        if args.dry_run:
            logger.info(f"[DRY RUN] Would run: {' '.join(cmd)}", iso_path)
            if tid == fpl_identified_main_tid:
                logger.info(f"[DRY RUN] Would rename output to main_title.mkv", iso_path)
            continue

        # --- Free-space check (safety enhancement 3) ---
        # Checked per-title, not just once per ISO, since free space keeps
        # dropping across a multi-title disc. title.size_bytes (MakeMKV's
        # own reported size for that title) is used as the required-space
        # estimate.
        required_bytes = title.size_bytes if title.size_bytes > 0 else iso_path.stat().st_size
        space_error = check_free_space(out_dir, output_root, required_bytes, args.free_space_margin_pct)
        if space_error:
            logger.error(f"Title {tid}: {space_error} - stopping this ISO", iso_path)
            stats.conversions_error += 1
            all_ok = False
            break  # further titles for this ISO won't fare any better

        before_snapshot = snapshot_output_dir(out_dir)
        progress_cb = make_progress_printer(iso_path.name, tid)
        rc, mkv_output = run_cmd_with_progress(cmd, on_progress=progress_cb)
        if progress_cb is not None:
            sys.stdout.write("\n")
            sys.stdout.flush()

        if rc != 0:
            logger.error(f"makemkvcon failed for title {tid} (exit code {rc})", iso_path)
            logger.append_raw_to_file(mkv_output)
            stats.conversions_error += 1
            all_ok = False
            continue

        # --- Verify output actually exists before trusting "success" (safety enhancement 1) ---
        after_snapshot = snapshot_output_dir(out_dir)
        new_or_changed = [
            name for name, size in after_snapshot.items()
            if before_snapshot.get(name) != size
        ]
        qualifying_new_mkvs = sorted(
            (name for name in new_or_changed
             if name.lower().endswith(".mkv") and after_snapshot[name] >= MIN_OUTPUT_FILE_BYTES),
            key=lambda name: after_snapshot[name],
            reverse=True,
        )
        if not qualifying_new_mkvs:
            logger.error(
                f"makemkvcon reported success for title {tid} but no new/updated .mkv file "
                f"of meaningful size was found in {out_dir} - treating as a failure",
                iso_path,
            )
            logger.append_raw_to_file(mkv_output)
            stats.conversions_error += 1
            all_ok = False
            continue
        if len(qualifying_new_mkvs) > 1:
            logger.warning(
                f"Title {tid}: expected one output .mkv file but found {len(qualifying_new_mkvs)} - "
                f"using only the largest ({qualifying_new_mkvs[0]})",
                iso_path,
            )
            stats.warnings += 1

        warn_line = looks_like_warning(mkv_output)
        if warn_line:
            logger.warning(f"Title {tid}: {warn_line}", iso_path)
            stats.warnings += 1

        # If MakeMKV's own (FPL_MainFeature) analysis identified this title
        # as the main feature, name its output "main_title.mkv" so other
        # scripts (organize_media.py, HandBrake batch scripts, etc.) can
        # trust the filename instead of re-deriving which title was main.
        # Not done for this script's own duration-based fallback guesses -
        # only for MakeMKV's own identification.
        if tid == fpl_identified_main_tid:
            src_path = out_dir / qualifying_new_mkvs[0]
            dest_path = out_dir / "main_title.mkv"
            if src_path != dest_path:
                if dest_path.exists():
                    logger.warning(
                        f"Title {tid}: wanted to name output 'main_title.mkv' but that file "
                        f"already exists in {out_dir} - leaving it as {qualifying_new_mkvs[0]}",
                        iso_path,
                    )
                    stats.warnings += 1
                else:
                    try:
                        src_path.replace(dest_path)
                        logger.info(f"Title {tid}: renamed output to main_title.mkv", iso_path)
                    except OSError as e:
                        logger.warning(f"Title {tid}: failed to rename output to main_title.mkv: {e}", iso_path)
                        stats.warnings += 1

        stats.conversions_success += 1

    if not all_ok:
        logger.error("One or more titles failed to convert; source file retained", iso_path)
        return ProcessResult()

    logger.info("All selected titles converted successfully", iso_path)
    stats.isos_converted += 1

    if args.keep_source:
        logger.info("Keeping source file (--keep-source=True)", iso_path)
    else:
        if args.dry_run:
            logger.info("[DRY RUN] Would delete source ISO file", iso_path)
        else:
            try:
                iso_path.unlink()
                logger.info("Deleted source ISO file", iso_path)
            except OSError as e:
                logger.error(f"Failed to delete source file: {e}", iso_path)

    return ProcessResult(converted=True)


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

def compile_regex_arg(value: str) -> re.Pattern:
    try:
        return re.compile(value)
    except re.error as e:
        raise argparse.ArgumentTypeError(f"Invalid regular expression {value!r}: {e}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Batch-convert .iso files to .mkv using makemkvcon.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-i", "--input", default=".", help="Input folder to search recursively for .iso files")
    p.add_argument("-o", "--output", default=".", help="Output root folder")
    p.add_argument(
        "--min-length", type=float, default=6.0, metavar="MINUTES",
        help="Minimum title length (in minutes) to extract",
    )
    p.add_argument(
        "-l", "--log", nargs="?", const=DEFAULT_LOG, default=DEFAULT_LOG, metavar="LOGFILE",
        help="Log file path",
    )
    p.add_argument(
        "--keep-source", type=str2bool, default=False, metavar="{True,False}",
        help="Keep the source ISO after a successful conversion instead of deleting it",
    )
    p.add_argument("--dry-run", action="store_true", help="Show what would happen without changing anything")
    p.add_argument(
        "--limit", type=float, default=-1, metavar="GB",
        help="Stop once this many GB of ISO source data have been converted (-1 = no limit)",
    )
    filter_group = p.add_mutually_exclusive_group()
    filter_group.add_argument(
        "--include", type=compile_regex_arg, default=None, metavar="REGEX",
        help="Only process ISOs whose full path matches this regex; all others are skipped. "
             "Mutually exclusive with --exclude",
    )
    filter_group.add_argument(
        "--exclude", type=compile_regex_arg, default=None, metavar="REGEX",
        help="Skip any ISO whose full path matches this regex; all others are processed. "
             "Mutually exclusive with --include",
    )
    p.add_argument(
        "--obfuscation-threshold", type=int, default=100, metavar="N",
        help="Number of same-duration titles that triggers Playlist Obfuscation suspicion",
    )
    p.add_argument(
        "--dvd-max-size-gb", type=float, default=DVD_MAX_SIZE_GB_DEFAULT, metavar="GB",
        help="ISOs at or below this size (decimal GB) are classified as DVD; larger ones as Blu-ray",
    )
    p.add_argument(
        "--disc-type", choices=["auto", "dvd", "bluray"], default="auto",
        help="Force disc-type classification for this run instead of using the size heuristic",
    )
    p.add_argument(
        "--detect-playall", type=str2bool, default=True, metavar="{True,False}",
        help="Detect and exclude a 'Play All' concatenation title (common on TV-show DVDs)",
    )
    p.add_argument(
        "--playall-tolerance-sec", type=float, default=30.0, metavar="SECONDS",
        help="Base tolerance (in seconds) between the longest title's duration and the summed "
             "duration of the episode-like cluster of other titles, to call it a 'Play All' title",
    )
    p.add_argument(
        "--playall-cluster-tolerance-pct", type=float, default=30.0, metavar="PCT",
        help="How far (as a %% of the median) another title's duration may be from its peers "
             "to still be grouped into the 'episode' cluster for Play All detection",
    )
    p.add_argument(
        "-f", "--force", action="store_true",
        help="Redo an ISO even if its output folder already looks fully converted "
             "(default: skip it)",
    )
    p.add_argument(
        "--free-space-margin-pct", type=float, default=10.0, metavar="PCT",
        help="Extra safety margin (as %% of a title's estimated size) required as free space "
             "on the output volume before extracting that title",
    )
    p.add_argument(
        "--max-consecutive-failures", type=int, default=3, metavar="N",
        help="Abort the whole run if this many ISOs in a row fail at the initial title-info "
             "scan (a strong sign of a systemic problem - bad makemkvcon path, expired key, "
             "permissions - rather than one bad disc). 0 disables this circuit breaker",
    )
    p.add_argument(
        "--makemkvcon", default="makemkvcon", metavar="PATH",
        help="Path to the makemkvcon executable",
    )
    return p.parse_args()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    input_root = Path(args.input).resolve()
    output_root = Path(args.output).resolve()
    log_path = Path(args.log).resolve()

    logger = DualLogger(log_path)

    if args.dry_run:
        logger.info("Running in DRY RUN mode - no files will be changed")

    # --- Pre-flight check (safety enhancement 2) ---
    # Fail fast on a bad/missing makemkvcon rather than letting every ISO
    # in the batch fail individually with the same root cause.
    preflight_error = preflight_check_makemkvcon(args.makemkvcon)
    if preflight_error:
        logger.error(preflight_error)
        logger.error("Aborting before processing any files - check --makemkvcon or your PATH")
        logger.close()
        return 1

    if not input_root.is_dir():
        logger.error(f"Input folder does not exist: {input_root}")
        logger.close()
        return 1

    if not args.dry_run:
        output_root.mkdir(parents=True, exist_ok=True)

    iso_files = sorted(
        {p for p in input_root.rglob("*") if p.is_file() and p.suffix.lower() == ".iso"}
    )

    if args.include:
        before = len(iso_files)
        iso_files = [p for p in iso_files if args.include.search(str(p))]
        logger.info(
            f"--include={args.include.pattern!r} applied: {len(iso_files)} of {before} "
            f"ISO(s) matched and will be processed"
        )
    elif args.exclude:
        before = len(iso_files)
        iso_files = [p for p in iso_files if not args.exclude.search(str(p))]
        logger.info(
            f"--exclude={args.exclude.pattern!r} applied: {before - len(iso_files)} of {before} "
            f"ISO(s) matched and will be skipped"
        )

    if not iso_files:
        logger.info(f"No .iso files found under {input_root}")
        logger.close()
        return 0

    total_bytes_all = sum(p.stat().st_size for p in iso_files)
    total_count = len(iso_files)
    limit_bytes = args.limit * (1024 ** 3) if args.limit and args.limit > 0 else None

    logger.info(
        f"Found {total_count} ISO file(s), {human_bytes(total_bytes_all)} total, "
        f"under {input_root}"
    )

    stats = Stats()
    used_output_names: set = set()

    start_time = time.time()
    bytes_time_processed = 0
    bytes_converted_running = 0
    consecutive_info_scan_failures = 0
    interrupted = False

    try:
        for idx, iso_path in enumerate(iso_files, start=1):
            if limit_bytes is not None and bytes_converted_running >= limit_bytes:
                logger.info(
                    f"Byte limit reached ({human_bytes(bytes_converted_running)} >= "
                    f"{human_bytes(limit_bytes)}) - stopping"
                )
                break

            elapsed = time.time() - start_time
            if bytes_time_processed > 0:
                rate = elapsed / bytes_time_processed  # seconds per byte
                remaining_bytes = max(total_bytes_all - bytes_time_processed, 0)
                eta_str = format_duration(rate * remaining_bytes)
            else:
                eta_str = "calculating..."

            iso_size = iso_path.stat().st_size
            print(
                f"[{idx}/{total_count}] {iso_path.name} ({human_bytes(iso_size)}) "
                f"- estimated time remaining: {eta_str}"
            )

            result = process_iso(iso_path, output_root, args, logger, stats, used_output_names)

            # --- Circuit breaker (safety enhancement 2, cont'd) ---
            # A single bad disc failing to scan is normal; several in a
            # row almost always means something systemic (expired
            # registration key, permissions, wrong binary) rather than
            # unlucky media, so stop instead of burning through the rest
            # of the batch with the same failure.
            if result.info_scan_failed:
                consecutive_info_scan_failures += 1
                if (
                    args.max_consecutive_failures > 0
                    and consecutive_info_scan_failures >= args.max_consecutive_failures
                ):
                    logger.error(
                        f"{consecutive_info_scan_failures} ISOs in a row failed at the title-info "
                        f"scan step - this usually means a systemic problem (makemkvcon "
                        f"registration/key, permissions, or a bad --makemkvcon path) rather than "
                        f"bad discs. Stopping early; fix the underlying issue and re-run "
                        f"(already-converted ISOs will be skipped via resume support)."
                    )
                    break
            else:
                consecutive_info_scan_failures = 0

            bytes_time_processed += iso_size
            if result.converted:
                bytes_converted_running += iso_size
                stats.bytes_converted += iso_size

    except KeyboardInterrupt:
        interrupted = True
        print()  # in case a live progress line was mid-write
        logger.warning("Interrupted by user (Ctrl+C) - stopping and printing the summary so far")

    summary_lines = [
        "",
        "==================== SUMMARY ====================",
        f"Successful title conversions : {stats.conversions_success}",
        f"Conversion errors            : {stats.conversions_error}",
        f"Conversion warnings          : {stats.warnings}",
        f"ISO files converted          : {stats.isos_converted}",
        f"Already-converted skipped    : {stats.already_converted_skipped}",
        f"Total ISO bytes converted    : {human_bytes(stats.bytes_converted)}",
        "==================================================",
    ]
    for line in summary_lines:
        print(line)
        logger.append_raw_to_file(line)

    logger.close()
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
