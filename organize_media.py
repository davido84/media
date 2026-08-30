#!/usr/bin/env python3
"""
organize_media.py
==================

Rename already-extracted .mkv files (e.g. produced by iso_to_mkv.py, or
ripped/encoded some other way) into Jellyfin/Plex-friendly names, for any
output folder whose name matches the "<Title> (<Year>)" convention.

This is deliberately a SEPARATE script from iso_to_mkv.py. Naming and
organizing a media library is a different problem from disc extraction,
and keeping them apart means:
  - this script can be re-run as many times as you like with zero risk to
    source ISOs or the slow, disc-dependent extraction pipeline
  - it works on ANY .mkv files sitting in a matching folder, not just ones
    iso_to_mkv.py itself produced
  - future work here (multi-disc consolidation, TV episode naming) never
    has to be threaded through disc-extraction code

-----------------------------------------------------------------------------
HOW IT WORKS
-----------------------------------------------------------------------------
1. Recursively finds every directory (at any depth under --input) whose
   name matches "<Title> (<Year>)" exactly - e.g. "Inception (2010)".
   (Scope is deliberately a single folder's own name for now, same as
   iso_to_mkv.py's current --movie-naming scope was - not an ancestor's
   name. Extending that, and consolidating a movie's multiple per-disc
   subfolders into one, are planned follow-ups, along with TV episode
   naming - none of that is implemented yet.)

2. Within each matching directory, every .mkv file DIRECTLY inside it
   (not recursing further - a "Featurettes/" subfolder or similar is left
   alone) has its duration probed with ffprobe.

3. The single longest .mkv is treated as the main feature and renamed to
   "<Title> (<Year>).mkv". Every other .mkv in that folder is renamed
   "extra.<n>.mkv", numbered longest-to-shortest.

4. A file already at its correct target name is left untouched (a no-op),
   so re-running this script is safe and idempotent as long as the set of
   files in the folder hasn't changed since the last run.

-----------------------------------------------------------------------------
CAVEATS
-----------------------------------------------------------------------------
- Requires ffprobe (part of ffmpeg) on PATH, or pointed to explicitly via
  --ffprobe. This is checked up front, before any files are touched.
- "Main = longest file" can't distinguish a real extra from a second cut
  of the movie itself (e.g. theatrical vs extended edition, both
  genuinely long). If the two longest files in a folder are within
  --similar-duration-pct of each other, a WARNING is logged so you can
  check by hand - the longest still wins by default rather than the run
  stalling on it.
- This only reorganizes ONE folder's contents at a time. A movie split
  across multiple per-disc ISOs living in their own subfolders is NOT
  consolidated into one output folder by this script - that's a planned
  follow-up, not yet implemented.
- Renames use os.replace (atomic, overwrites cleanly on both POSIX and
  Windows) - but this script will never rename a file onto an EXISTING,
  DIFFERENT file (i.e. it never silently clobbers unrelated content);
  that case is logged as an error and skipped rather than acted on.
- Characters illegal in Windows/exFAT/SMB filenames are stripped from the
  title before use, since ripped titles frequently end up served to a
  media library over a network share regardless of the ripping OS.
"""

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_LOG = "./organize.log"

# Matches a Jellyfin/Plex-style "<Title> (<Year>)" folder name.
MOVIE_TITLE_YEAR_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<year>(?:19|20)\d{2})\)\s*$")

# Characters not safely usable in filenames on at least one common
# filesystem (Windows/exFAT is the strictest of the platforms this is
# likely to end up served from).
INVALID_FILENAME_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename_component(name: str) -> str:
    cleaned = INVALID_FILENAME_CHARS_RE.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "output"


# --------------------------------------------------------------------------
# Logging - same shape as iso_to_mkv.py's DualLogger, kept independent
# on purpose since these are separate, standalone scripts.
# --------------------------------------------------------------------------

class Logger:
    def __init__(self, log_path: Optional[Path]):
        self._fh = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(log_path, "a", encoding="utf-8")
            self._raw(f"\n==== Run started {datetime.now().isoformat(timespec='seconds')} ====")

    def _raw(self, line: str) -> None:
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def _log(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} [{level}] {message}"
        print(line)
        self._raw(line)

    def info(self, message: str) -> None:
        self._log("INFO", message)

    def warning(self, message: str) -> None:
        self._log("WARNING", message)

    def error(self, message: str) -> None:
        self._log("ERROR", message)

    def close(self) -> None:
        if self._fh:
            self._fh.close()


# --------------------------------------------------------------------------
# ffprobe interaction
# --------------------------------------------------------------------------

def preflight_check_ffprobe(ffprobe_bin: str) -> Optional[str]:
    if shutil.which(ffprobe_bin) is None:
        return f"ffprobe executable not found or not executable: {ffprobe_bin!r}"
    return None


def probe_duration_seconds(ffprobe_bin: str, path: Path) -> Optional[float]:
    cmd = [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Core logic
# --------------------------------------------------------------------------

@dataclass
class Stats:
    folders_matched: int = 0
    files_renamed: int = 0
    files_already_correct: int = 0
    errors: int = 0
    warnings: int = 0


def find_movie_folders(input_root: Path) -> List[Path]:
    return sorted(
        p for p in input_root.rglob("*")
        if p.is_dir() and MOVIE_TITLE_YEAR_RE.match(p.name.strip())
    )


def rename_one(src: Path, dest: Path, args: argparse.Namespace, logger: Logger, stats: Stats) -> None:
    if src == dest:
        stats.files_already_correct += 1
        return
    if dest.exists():
        logger.error(f"Cannot rename {src.name} -> {dest.name}: a different file already exists at that name")
        stats.errors += 1
        return
    if args.dry_run:
        logger.info(f"[DRY RUN] Would rename {src} -> {dest.name}")
        stats.files_renamed += 1
        return
    try:
        src.replace(dest)
        logger.info(f"Renamed {src} -> {dest.name}")
        stats.files_renamed += 1
    except OSError as e:
        logger.error(f"Failed to rename {src} -> {dest.name}: {e}")
        stats.errors += 1


def organize_folder(folder: Path, args: argparse.Namespace, logger: Logger, stats: Stats) -> None:
    m = MOVIE_TITLE_YEAR_RE.match(folder.name.strip())
    assert m is not None
    title = m.group("title").strip()
    year = m.group("year")
    canonical_stem = sanitize_filename_component(f"{title} ({year})")

    mkvs = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".mkv")
    if not mkvs:
        return

    stats.folders_matched += 1
    logger.info(f"{folder}: found {len(mkvs)} .mkv file(s)")

    durations: List[Tuple[Path, float]] = []
    for f in mkvs:
        dur = probe_duration_seconds(args.ffprobe, f)
        if dur is None:
            logger.warning(f"{folder}: could not determine duration of {f.name} (ffprobe failed) - treating as 0s")
            stats.warnings += 1
            dur = 0.0
        durations.append((f, dur))

    durations.sort(key=lambda item: item[1], reverse=True)

    if len(durations) >= 2:
        longest, second = durations[0][1], durations[1][1]
        if longest > 0 and (longest - second) / longest * 100.0 < args.similar_duration_pct:
            logger.warning(
                f"{folder}: the two longest files are within {args.similar_duration_pct:g}% of each "
                f"other in duration ({durations[0][0].name}: {longest:.0f}s vs {durations[1][0].name}: "
                f"{second:.0f}s) - picking the longest as the main feature, but this may actually be "
                f"two different cuts of the movie (theatrical vs extended, etc) - please double check"
            )
            stats.warnings += 1

    main_file, _ = durations[0]
    rename_one(main_file, folder / f"{canonical_stem}.mkv", args, logger, stats)

    for n, (f, _dur) in enumerate(durations[1:], start=1):
        rename_one(f, folder / f"extra.{n}.mkv", args, logger, stats)


# --------------------------------------------------------------------------
# Argument parsing / main
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Rename extracted .mkv files into Jellyfin/Plex-friendly names.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "-i", "--input", default=".",
        help="Root folder to search recursively for '<Title> (<Year>)' folders",
    )
    p.add_argument(
        "-l", "--log", nargs="?", const=DEFAULT_LOG, default=DEFAULT_LOG, metavar="LOGFILE",
        help="Log file path",
    )
    p.add_argument("--dry-run", action="store_true", help="Show what would be renamed without changing anything")
    p.add_argument(
        "--similar-duration-pct", type=float, default=10.0, metavar="PCT",
        help="Warn if the two longest files in a folder are within this %% of each other in "
             "duration (possible second cut of the movie, not a real extra)",
    )
    p.add_argument("--ffprobe", default="ffprobe", metavar="PATH", help="Path to the ffprobe executable")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    input_root = Path(args.input).resolve()
    log_path = Path(args.log).resolve()
    logger = Logger(log_path)

    if args.dry_run:
        logger.info("Running in DRY RUN mode - no files will be changed")

    preflight_error = preflight_check_ffprobe(args.ffprobe)
    if preflight_error:
        logger.error(preflight_error)
        logger.error("Aborting before processing any files - install ffmpeg/ffprobe or pass --ffprobe")
        logger.close()
        return 1

    if not input_root.is_dir():
        logger.error(f"Input folder does not exist: {input_root}")
        logger.close()
        return 1

    folders = find_movie_folders(input_root)
    logger.info(f"Found {len(folders)} folder(s) matching '<Title> (<Year>)' under {input_root}")

    stats = Stats()
    for folder in folders:
        organize_folder(folder, args, logger, stats)

    summary_lines = [
        "",
        "==================== SUMMARY ====================",
        f"Movie folders processed      : {stats.folders_matched}",
        f"Files renamed                : {stats.files_renamed}",
        f"Files already correctly named: {stats.files_already_correct}",
        f"Warnings                     : {stats.warnings}",
        f"Errors                       : {stats.errors}",
        "==================================================",
    ]
    for line in summary_lines:
        print(line)
        logger._raw(line)

    logger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
