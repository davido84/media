#!/usr/bin/env python3
"""
organize_media.py
==================

Reorganize already-extracted .mkv files (e.g. produced by iso_to_mkv.py, or
ripped/encoded some other way) into Jellyfin/Plex-friendly names and folder
structure, for any top-level folder whose name matches the
"<Title> (<Year>)" convention.

A single top-level "<Title> (<Year>)" folder is treated as EITHER a movie or a
TV series, decided by its contents:

  - If it contains one or more "<S>-<D>" disk subfolders (season-disk, e.g.
    "1-1", "1-2", "2-1"), it is treated as a TV SERIES and reorganized into
    Jellyfin's "Season NN/<Title> (<Year>) SNNENN.mkv" layout.

  - Otherwise, if it contains .mkv files directly, it is treated as a MOVIE
    and its files are renamed in place (longest = main feature, rest =
    extras).

Internally the script works in two phases: it first builds a complete PLAN of
every rename/move/removal without touching the disk, then either RENDERS that
plan (--dry-run) or EXECUTES it. Because both paths consume the identical
plan, a --dry-run preview is a faithful description of exactly what a real run
would do - including which operations would be skipped due to conflicts.

This is deliberately a SEPARATE script from iso_to_mkv.py. Naming and
organizing a media library is a different problem from disc extraction, and
keeping them apart means this script can be re-run freely with zero risk to
source ISOs or the slow extraction pipeline.

-----------------------------------------------------------------------------
HOW IT WORKS
-----------------------------------------------------------------------------
1. Recursively finds every directory (at any depth under --input) whose name
   matches "<Title> (<Year>)" exactly - e.g. "Inception (2010)" or
   "Breaking Bad (2008)".

2. Each such folder is classified:
     TV SERIES  if it directly contains any "<S>-<D>" disk subfolder
     MOVIE      otherwise

   --- MOVIE ---
   The longest .mkv directly inside becomes "<Title> (<Year>).mkv"; the rest
   become "extra.<n>.mkv", numbered longest-to-shortest. (A "Featurettes/"
   subfolder or similar is not recursed into.)

   --- TV SERIES ---
   Disk subfolders are grouped by season; within a season disks are ordered by
   disk number and each disk's "title_<nn>.mkv" files by title number. If
   extras detection is on (default), short outlier titles are separated out as
   bonus extras (see EXTRAS DETECTION). The remaining titles are numbered
   sequentially across the whole season - disk 1's episodes first, then disk
   2's continuing the count - and moved to
   "Season <NN>/<Title> (<Year>) S<NN>E<NN>.mkv". Detected extras go to
   "Season <NN>/extras/" (Jellyfin's generic-extras folder). Emptied "<S>-<D>"
   disk folders are removed unless --keep-empty-dirs is given.

3. Re-running is safe and idempotent: a file already at its target name is a
   no-op, and once a series' disk folders have been consumed into Season
   folders a re-run finds nothing to do.

-----------------------------------------------------------------------------
EXTRAS DETECTION (TV)
-----------------------------------------------------------------------------
A raw disc rip carries no metadata to say "this title is a featurette", so the
only signal is running time. The rules are deliberately conservative:

  - The reference "typical episode length" is the MEDIAN duration of all
    titles in the season, pooled across every disk (median because it shrugs
    off a handful of outliers, as long as real episodes are the majority).
  - A title is separated out as an extra only if SHORTER than
    --extra-threshold-pct of that median (default 50%).
  - A much LONGER title is NOT auto-classified as an extra (it is usually a
    two-part/finale episode) - it stays an episode but is flagged for review.
  - SAFETY NET: if half or more of a season's titles fall under the threshold,
    the "episodes are the majority" assumption has failed; detection backs off
    for that season and treats everything as an episode, with a warning.
  - A season with fewer than 3 probeable titles skips detection entirely.

Turn detection off with --no-detect-extras (TV then needs no ffprobe).

-----------------------------------------------------------------------------
CAVEATS
-----------------------------------------------------------------------------
- ffprobe (part of ffmpeg) is needed for MOVIE folders always, and for TV
  folders when extras detection is on. Checked up front, only if the run
  needs it; override its location with --ffprobe.
- MOVIES: "main = longest" can't tell a real extra from a second cut
  (theatrical vs extended). If the two longest are within
  --similar-duration-pct, a warning is logged; the longest still wins.
- Only "title_<nn>.mkv" files in a disk folder are considered episodes/extras;
  any other .mkv there is left in place with a warning.
- Moves use os.replace (atomic) but never overwrite an EXISTING, DIFFERENT
  file; such a collision is reported and skipped, never acted on.
- Characters illegal in Windows/exFAT/SMB filenames are stripped from the
  title before use.
"""

import argparse
import re
import shutil
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_LOG = "./organize.log"

# Matches a Jellyfin/Plex-style "<Title> (<Year>)" folder name.
TITLE_YEAR_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<year>(?:19|20)\d{2})\)\s*$")
# Matches a per-disc subfolder "<season>-<disk>", e.g. "1-1", "2-3", "10-1".
SEASON_DISK_RE = re.compile(r"^(?P<season>\d+)-(?P<disk>\d+)$")
# Matches an extracted title file "title_<nn>.mkv", e.g. "title_00.mkv".
EPISODE_FILE_RE = re.compile(r"^title_(?P<num>\d+)\.mkv$", re.IGNORECASE)
# Characters not safely usable in filenames on Windows/exFAT/SMB.
INVALID_FILENAME_CHARS_RE = re.compile(r'[\\/:*?"<>|]')

# --- TV extras-detection tuning (season-relative, duration-based) ---
EXTRA_DETECTION_MIN_TITLES = 3   # need >= this many probeable titles to detect
LONG_EPISODE_RATIO = 1.75        # > this * median -> flag as possible two-parter
EXTRA_BORDERLINE_BAND = 0.10     # within this ratio of the threshold -> "verify"


def sanitize_filename_component(name: str) -> str:
    cleaned = INVALID_FILENAME_CHARS_RE.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "output"


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------
# Logging
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
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {message}"
        print(line)
        self._raw(line)

    def info(self, message: str) -> None:
        self._log("INFO", message)

    def warning(self, message: str) -> None:
        self._log("WARNING", message)

    def error(self, message: str) -> None:
        self._log("ERROR", message)

    def report(self, line: str = "") -> None:
        # Un-timestamped output for the dry-run report; printed and logged.
        print(line)
        self._raw(line)

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
# Plan data model
# --------------------------------------------------------------------------

# Move.status is set during conflict resolution:
#   ""       -> will be performed
#   "noop"   -> src is already at dest (already correctly named)
#   "exists" -> a different file already occupies dest (skip)
#   "dup"    -> another planned move also targets dest (skip)
#   "chain"  -> dest exists and is itself a file being moved (skip; ordering hazard)
SKIP_STATUSES = ("exists", "dup", "chain")


@dataclass
class PlannedMove:
    src: Path
    dest: Path
    kind: str                       # episode | extra | movie-main | movie-extra
    duration: Optional[float] = None
    ratio: Optional[float] = None   # duration / season median (TV only)
    flag: str = ""                  # "" | long | borderline
    status: str = ""


@dataclass
class SeasonPlan:
    series_name: str
    season: int
    reference: Optional[float]
    detection_note: str
    moves: List[PlannedMove]


@dataclass
class MoviePlan:
    folder_name: str
    moves: List[PlannedMove]


@dataclass
class Plan:
    seasons: List[SeasonPlan] = field(default_factory=list)
    movies: List[MoviePlan] = field(default_factory=list)
    removals: List[Path] = field(default_factory=list)
    # disk folder -> (num title files, had non-title leftovers)
    disk_info: Dict[Path, Tuple[int, bool]] = field(default_factory=dict)

    def all_moves(self) -> List[PlannedMove]:
        out: List[PlannedMove] = []
        for sp in self.seasons:
            out.extend(sp.moves)
        for mp in self.movies:
            out.extend(mp.moves)
        return out


@dataclass
class Stats:
    movie_folders: int = 0
    series_folders: int = 0
    seasons: int = 0
    files_renamed: int = 0
    files_already_correct: int = 0
    extras_separated: int = 0
    dirs_removed: int = 0
    conflicts: int = 0
    errors: int = 0
    warnings: int = 0


# --------------------------------------------------------------------------
# Discovery / classification
# --------------------------------------------------------------------------

def find_title_year_folders(input_root: Path) -> List[Path]:
    return sorted(
        p for p in input_root.rglob("*")
        if p.is_dir() and TITLE_YEAR_RE.match(p.name.strip())
    )


def folder_has_season_disks(folder: Path) -> bool:
    try:
        return any(p.is_dir() and SEASON_DISK_RE.match(p.name.strip()) for p in folder.iterdir())
    except OSError:
        return False


def _collect_disk_titles(disk_path: Path, logger: Logger, stats: Stats) -> Tuple[List[Tuple[int, Path]], List[Path]]:
    """Return (titles, leftovers). titles = [(title_number, path)] sorted by number."""
    titles: List[Tuple[int, Path]] = []
    leftovers: List[Path] = []
    for f in sorted(disk_path.iterdir()):
        em = EPISODE_FILE_RE.match(f.name) if f.is_file() else None
        if em:
            titles.append((int(em.group("num")), f))
        else:
            leftovers.append(f)
            if f.is_file() and f.suffix.lower() == ".mkv":
                logger.warning(f"{disk_path}: '{f.name}' does not match 'title_<nn>.mkv' - "
                               f"not treated as an episode, left in place")
                stats.warnings += 1
    titles.sort(key=lambda item: item[0])
    return titles, leftovers


# --------------------------------------------------------------------------
# Planning: TV
# --------------------------------------------------------------------------

def _plan_season(series_folder: Path, series_stem: str, season: int, disks: List[Tuple[int, Path]],
                 args: argparse.Namespace, logger: Logger, stats: Stats, plan: Plan) -> Optional[SeasonPlan]:
    season_label = f"{series_folder.name} / Season {season:02d}"
    season_folder = series_folder / f"Season {season:02d}"
    extras_folder = season_folder / "extras"

    ordered: List[Path] = []
    for _disk_num, disk_path in disks:
        titles, leftovers = _collect_disk_titles(disk_path, logger, stats)
        plan.disk_info[disk_path] = (len(titles), bool(leftovers))
        ordered.extend(p for _n, p in titles)
    if not ordered:
        return None

    durations: Dict[Path, Optional[float]] = {}
    ratios: Dict[Path, Optional[float]] = {p: None for p in ordered}
    flags: Dict[Path, str] = {p: "" for p in ordered}
    is_extra: Dict[Path, bool] = {p: False for p in ordered}
    reference: Optional[float] = None
    detection_note = ""

    if not args.detect_extras:
        detection_note = "extras detection off - every title treated as an episode"
    else:
        for p in ordered:
            durations[p] = probe_duration_seconds(args.ffprobe, p)
        known = [d for p in ordered if (d := durations[p]) is not None and d > 0]
        if len(known) < EXTRA_DETECTION_MIN_TITLES:
            detection_note = (f"only {len(known)} probeable title(s) - too few to detect extras; "
                              f"all treated as episodes")
        else:
            reference = statistics.median(known)
            extra_ratio = args.extra_threshold_pct / 100.0
            for p in ordered:
                d = durations[p]
                if d is None or d <= 0:
                    logger.warning(f"{season_label}: could not determine duration of '{p.name}' - "
                                   f"keeping it as an episode")
                    stats.warnings += 1
                    continue
                ratios[p] = d / reference
                if ratios[p] < extra_ratio:
                    is_extra[p] = True

            n_extra = sum(is_extra.values())
            if n_extra and n_extra >= (len(ordered) - n_extra):
                logger.warning(f"{season_label}: {n_extra} of {len(ordered)} titles fell under the extras "
                               f"threshold - too many for the 'episodes are the majority' assumption; "
                               f"ignoring duration-based detection here and treating all as episodes")
                stats.warnings += 1
                detection_note = (f"detection backed off - {n_extra}/{len(ordered)} titles under threshold "
                                  f"(assumption broke); all treated as episodes")
                is_extra = {p: False for p in ordered}
            else:
                for p in ordered:
                    r = ratios[p]
                    if r is None:
                        continue
                    if abs(r - extra_ratio) < EXTRA_BORDERLINE_BAND:
                        flags[p] = "borderline"
                    elif not is_extra[p] and r > LONG_EPISODE_RATIO:
                        flags[p] = "long"
                detection_note = (f"typical episode {format_duration(reference)}; "
                                  f"extra if < {format_duration(reference * extra_ratio)} "
                                  f"({args.extra_threshold_pct:g}%)")

    moves: List[PlannedMove] = []
    episode_no = 0
    extra_no = 0
    for p in ordered:
        if is_extra[p]:
            extra_no += 1
            dest = extras_folder / f"{series_stem} - Extra {extra_no:02d}.mkv"
            moves.append(PlannedMove(p, dest, "extra", durations.get(p), ratios.get(p), flags.get(p, "")))
        else:
            episode_no += 1
            dest = season_folder / f"{series_stem} S{season:02d}E{episode_no:02d}.mkv"
            moves.append(PlannedMove(p, dest, "episode", durations.get(p), ratios.get(p), flags.get(p, "")))

    stats.seasons += 1
    return SeasonPlan(series_folder.name, season, reference, detection_note, moves)


def plan_tv_series(series_folder: Path, args: argparse.Namespace, logger: Logger, stats: Stats, plan: Plan) -> None:
    m = TITLE_YEAR_RE.match(series_folder.name.strip())
    assert m is not None
    title, year = m.group("title").strip(), m.group("year")
    series_stem = sanitize_filename_component(f"{title} ({year})")

    seasons: Dict[int, List[Tuple[int, Path]]] = {}
    stray_mkvs: List[Path] = []
    for p in series_folder.iterdir():
        if p.is_dir():
            sd = SEASON_DISK_RE.match(p.name.strip())
            if sd:
                seasons.setdefault(int(sd.group("season")), []).append((int(sd.group("disk")), p))
        elif p.is_file() and p.suffix.lower() == ".mkv":
            stray_mkvs.append(p)

    if not seasons:
        return
    stats.series_folders += 1
    if stray_mkvs:
        logger.warning(f"{series_folder}: {len(stray_mkvs)} .mkv file(s) sit directly in the series folder "
                       f"(outside any '<S>-<D>' disk folder) - left untouched")
        stats.warnings += 1

    for season in sorted(seasons):
        disks = sorted(seasons[season], key=lambda item: item[0])
        sp = _plan_season(series_folder, series_stem, season, disks, args, logger, stats, plan)
        if sp is not None:
            plan.seasons.append(sp)


# --------------------------------------------------------------------------
# Planning: movies
# --------------------------------------------------------------------------

def plan_movie_folder(folder: Path, args: argparse.Namespace, logger: Logger, stats: Stats) -> Optional[MoviePlan]:
    m = TITLE_YEAR_RE.match(folder.name.strip())
    assert m is not None
    title, year = m.group("title").strip(), m.group("year")
    canonical_stem = sanitize_filename_component(f"{title} ({year})")

    mkvs = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".mkv")
    if not mkvs:
        return None
    stats.movie_folders += 1

    probed: List[Tuple[Path, Optional[float]]] = []
    for f in mkvs:
        d = probe_duration_seconds(args.ffprobe, f)
        if d is None:
            logger.warning(f"{folder}: could not determine duration of {f.name} (ffprobe failed) - treating as 0s")
            stats.warnings += 1
        probed.append((f, d))
    probed.sort(key=lambda item: (item[1] or 0.0), reverse=True)

    if len(probed) >= 2:
        longest, second = probed[0][1] or 0.0, probed[1][1] or 0.0
        if longest > 0 and (longest - second) / longest * 100.0 < args.similar_duration_pct:
            logger.warning(f"{folder}: the two longest files are within {args.similar_duration_pct:g}% of each "
                           f"other ({probed[0][0].name}: {format_duration(longest)} vs {probed[1][0].name}: "
                           f"{format_duration(second)}) - picking the longest as the main feature, but this may "
                           f"be two cuts of the movie (theatrical vs extended); please double check")
            stats.warnings += 1

    moves: List[PlannedMove] = []
    main_file, main_dur = probed[0]
    moves.append(PlannedMove(main_file, folder / f"{canonical_stem}.mkv", "movie-main", main_dur))
    for n, (f, d) in enumerate(probed[1:], start=1):
        moves.append(PlannedMove(f, folder / f"extra.{n}.mkv", "movie-extra", d))
    return MoviePlan(folder.name, moves)


# --------------------------------------------------------------------------
# Conflict + removal resolution (operates on the whole plan, disk untouched)
# --------------------------------------------------------------------------

def resolve_statuses(plan: Plan) -> None:
    moves = plan.all_moves()
    dest_counts = Counter(m.dest for m in moves)
    srcs = {m.src for m in moves}
    for m in moves:
        if m.src == m.dest:
            m.status = "noop"
        elif dest_counts[m.dest] > 1:
            m.status = "dup"
        elif m.dest.exists():
            m.status = "chain" if m.dest in srcs else "exists"
        else:
            m.status = ""


def resolve_removals(plan: Plan, args: argparse.Namespace) -> None:
    if args.keep_empty_dirs:
        plan.removals = []
        return
    moves_by_disk: Dict[Path, List[PlannedMove]] = defaultdict(list)
    for sp in plan.seasons:
        for mv in sp.moves:
            moves_by_disk[mv.src.parent].append(mv)
    removable: List[Path] = []
    for disk, (n_titles, had_leftovers) in plan.disk_info.items():
        if n_titles > 0 and not had_leftovers and all(mv.status == "" for mv in moves_by_disk.get(disk, [])):
            removable.append(disk)
    plan.removals = sorted(removable)


# --------------------------------------------------------------------------
# Rendering (--dry-run)
# --------------------------------------------------------------------------

def _target_rel(move: PlannedMove) -> str:
    if move.kind == "extra":
        return f"extras/{move.dest.name}"
    return move.dest.name


def _tag_for(move: PlannedMove) -> str:
    if move.status == "noop":
        return "  (already named - no change)"
    if move.status == "exists":
        return "  [SKIP: a different file already exists at the target]"
    if move.status == "dup":
        return "  [SKIP: two files would land on this same target]"
    if move.status == "chain":
        return "  [SKIP: target is another file being moved this run]"
    if move.kind == "extra":
        base = "  [EXTRA]"
    elif move.kind == "movie-main":
        base = "  [main feature]"
    else:
        base = ""
    if move.flag == "long":
        base += "  [unusually long - verify it's not a two-parter/extra]"
    elif move.flag == "borderline":
        base += "  [borderline call - verify]"
    return base


def render_plan(plan: Plan, logger: Logger, input_root: Path) -> None:
    R = logger.report
    moves = plan.all_moves()
    to_do = [m for m in moves if m.status == ""]
    conflicts = [m for m in moves if m.status in SKIP_STATUSES]

    R()
    R("=================  DRY RUN  =================")
    R(f"Root: {input_root}")
    R("No files will be changed. This is exactly what a real run would do.")

    if not moves:
        R("")
        R("No '<Title> (<Year>)' folders with anything to organize were found.")
        R("=============================================")
        return
    if not to_do and not conflicts and not plan.removals:
        R("")
        R("Everything is already organized - nothing to do.")
        R("=============================================")
        return

    # ---- TV, grouped series -> season ----
    seasons_by_series: Dict[str, List[SeasonPlan]] = defaultdict(list)
    for sp in plan.seasons:
        seasons_by_series[sp.series_name].append(sp)

    for series_name, seasons in seasons_by_series.items():
        R("")
        R(f"[TV] {series_name}")
        for sp in sorted(seasons, key=lambda s: s.season):
            R(f"  Season {sp.season:02d}  ({sp.detection_note})")
            width = max((len(f"{m.src.parent.name}/{m.src.name}") for m in sp.moves), default=0)
            for m in sp.moves:
                src_disp = f"{m.src.parent.name}/{m.src.name}"
                pct = f"{m.ratio * 100:.0f}%" if m.ratio is not None else "  -"
                R(f"    {src_disp:<{width}}  {format_duration(m.duration):>8}  {pct:>4}  "
                  f"-> {_target_rel(m)}{_tag_for(m)}")

    # ---- Movies ----
    for mp in plan.movies:
        R("")
        R(f"[Movie] {mp.folder_name}")
        width = max((len(m.src.name) for m in mp.moves), default=0)
        for m in mp.moves:
            R(f"    {m.src.name:<{width}}  {format_duration(m.duration):>8}  "
              f"-> {m.dest.name}{_tag_for(m)}")

    # ---- Disk folders that would be removed ----
    if plan.removals:
        R("")
        R("Empty disk folders that would be removed:")
        for d in plan.removals:
            R(f"    {d.parent.name}/{d.name}")

    # ---- Conflicts, called out again together ----
    if conflicts:
        R("")
        R(f"!! {len(conflicts)} conflict(s) would be SKIPPED (nothing overwritten):")
        for m in conflicts:
            R(f"    {m.src}  ->  {m.dest.name}{_tag_for(m)}")

    R("")
    R("=============================================")


# --------------------------------------------------------------------------
# Execution (real run)
# --------------------------------------------------------------------------

def _dest_display(src: Path, dest: Path) -> str:
    return dest.name if dest.parent == src.parent else f"{dest.parent.name}/{dest.name}"


def execute_plan(plan: Plan, logger: Logger, stats: Stats) -> None:
    for m in plan.all_moves():
        shown = _dest_display(m.src, m.dest)
        if m.status == "noop":
            stats.files_already_correct += 1
            continue
        if m.status in SKIP_STATUSES:
            reason = {"exists": "a different file already exists at the target",
                      "dup": "two files target the same name",
                      "chain": "the target is another file being moved this run"}[m.status]
            logger.error(f"Skipping {m.src.name} -> {shown}: {reason}")
            stats.conflicts += 1
            continue
        try:
            m.dest.parent.mkdir(parents=True, exist_ok=True)
            m.src.replace(m.dest)
            logger.info(f"Renamed {m.src} -> {shown}")
            stats.files_renamed += 1
            if m.kind == "extra":
                stats.extras_separated += 1
            if m.flag == "long":
                logger.warning(f"{shown}: kept as an episode but unusually long "
                               f"({format_duration(m.duration)}) - verify it isn't a two-parter or extra")
                stats.warnings += 1
            elif m.flag == "borderline":
                logger.warning(f"{shown}: borderline episode/extra call "
                               f"({format_duration(m.duration)}) - verify")
                stats.warnings += 1
        except OSError as e:
            logger.error(f"Failed to rename {m.src} -> {shown}: {e}")
            stats.errors += 1

    for disk in plan.removals:
        try:
            disk.rmdir()
            logger.info(f"Removed empty disk folder {disk}")
            stats.dirs_removed += 1
        except OSError as e:
            logger.warning(f"Could not remove disk folder {disk}: {e}")
            stats.warnings += 1


def tally_plan_into_stats(plan: Plan, stats: Stats) -> None:
    """For --dry-run: make the summary reflect the plan without executing it."""
    for m in plan.all_moves():
        if m.status == "noop":
            stats.files_already_correct += 1
        elif m.status in SKIP_STATUSES:
            stats.conflicts += 1
        else:
            stats.files_renamed += 1
            if m.kind == "extra":
                stats.extras_separated += 1
    stats.dirs_removed += len(plan.removals)


# --------------------------------------------------------------------------
# Argument parsing / main
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Reorganize extracted .mkv files into Jellyfin/Plex-friendly names and layout.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-i", "--input", default=".",
                   help="Root folder to search recursively for '<Title> (<Year>)' folders")
    p.add_argument("-l", "--log", nargs="?", const=DEFAULT_LOG, default=DEFAULT_LOG, metavar="LOGFILE",
                   help="Log file path")
    p.add_argument("--dry-run", action="store_true",
                   help="Print exactly what a real run would do, changing nothing")
    p.add_argument("--keep-empty-dirs", action="store_true",
                   help="Do not remove '<S>-<D>' disk folders after their titles have been moved out")
    p.add_argument("--no-detect-extras", dest="detect_extras", action="store_false",
                   help="TV: don't use running time to separate 'extras' from episodes (TV then needs no ffprobe)")
    p.add_argument("--extra-threshold-pct", type=float, default=50.0, metavar="PCT",
                   help="TV: a title shorter than this %% of the season's median episode length is an extra")
    p.add_argument("--similar-duration-pct", type=float, default=10.0, metavar="PCT",
                   help="MOVIES: warn if the two longest files are within this %% of each other in duration")
    p.add_argument("--ffprobe", default="ffprobe", metavar="PATH", help="Path to the ffprobe executable")
    p.set_defaults(detect_extras=True)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    input_root = Path(args.input).resolve()
    log_path = Path(args.log).resolve()
    logger = Logger(log_path)

    if not input_root.is_dir():
        logger.error(f"Input folder does not exist: {input_root}")
        logger.close()
        return 1

    folders = find_title_year_folders(input_root)
    tv_series = [f for f in folders if folder_has_season_disks(f)]
    movies = [f for f in folders if f not in tv_series]
    logger.info(f"Found {len(folders)} '<Title> (<Year>)' folder(s) under {input_root} "
                f"({len(tv_series)} TV series, {len(movies)} movie)")

    need_ffprobe = bool(movies) or (bool(tv_series) and args.detect_extras)
    if need_ffprobe:
        err = preflight_check_ffprobe(args.ffprobe)
        if err:
            logger.error(err)
            logger.error("Aborting before touching any files - install ffmpeg/ffprobe, pass --ffprobe, "
                         "or (TV only) pass --no-detect-extras")
            logger.close()
            return 1

    stats = Stats()

    # --- Phase 1: plan everything (no disk changes) ---
    plan = Plan()
    for folder in tv_series:
        plan_tv_series(folder, args, logger, stats, plan)
    for folder in movies:
        mp = plan_movie_folder(folder, args, logger, stats)
        if mp is not None:
            plan.movies.append(mp)
    resolve_statuses(plan)
    resolve_removals(plan, args)

    # --- Phase 2: render (dry-run) or execute ---
    if args.dry_run:
        render_plan(plan, logger, input_root)
        tally_plan_into_stats(plan, stats)
    else:
        execute_plan(plan, logger, stats)

    def row(label: str, value: int) -> str:
        return f"{label:<29}: {value}"

    rename_label = "Files to rename/move" if args.dry_run else "Files renamed/moved"
    remove_label = "Empty disk folders to remove" if args.dry_run else "Empty disk folders removed"
    summary = [
        "",
        f"================ SUMMARY{' (DRY RUN)' if args.dry_run else ''} ================",
        row("TV series processed", stats.series_folders),
        row("Seasons processed", stats.seasons),
        row("Movie folders processed", stats.movie_folders),
        row(rename_label, stats.files_renamed),
        row("Files already correctly named", stats.files_already_correct),
        row("TV extras separated", stats.extras_separated),
        row(remove_label, stats.dirs_removed),
        row("Conflicts skipped", stats.conflicts),
        row("Warnings", stats.warnings),
        row("Errors", stats.errors),
        "=================================================",
    ]
    for line in summary:
        print(line)
        logger._raw(line)

    logger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
