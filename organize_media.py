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

The title folder may carry a metadata-provider id after the year, in either the
Plex/Sonarr/Radarr curly form ("{tvdb-78581}", "{imdb-tt0235198}", "{tmdb-N}")
or Jellyfin's own square form. Because Jellyfin only reads the square, id-suffixed
form, the id is normalized and the top-level folder is renamed accordingly:
"Dilbert (1999) {tvdb-78581}" -> "Dilbert (1999) [tvdbid-78581]". For a series the
id lives on the folder only (episode files stay id-free); for a movie it is also
added to the main file, e.g. "Audition (1999) [imdbid-tt0235198].mkv". Folders
already in the square form are left unchanged.

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
   A single-disc movie has its .mkv files directly in the folder: the longest
   becomes the main feature, named to match the folder (with the metadata id,
   if any): "<Title> (<Year>) [id].mkv". Every other .mkv is a bonus extra,
   moved into Jellyfin's "extras/" subfolder as "<Title> (<Year>) - Extra
   <NN>.mkv" (longest-to-shortest).

   A MULTI-DISC movie instead holds plain-numbered disc subfolders ("1", "2",
   ...), each containing "title_<nn>.mkv" files. The lowest-numbered disc is the
   main-feature disc and is handled exactly like a single-disc movie (longest =
   main, the rest = extras); every title on the higher discs is collected into
   "extras/" as well. Emptied disc folders are removed after organizing.

   --- TV SERIES ---
   Disk subfolders are grouped by season; within a season disks are ordered by
   disk number and each disk's "title_<nn>.mkv" files by title number. If
   extras detection is on (default), short outlier titles are separated out as
   bonus extras (see EXTRAS DETECTION). The remaining titles are numbered
   sequentially across the whole season - disk 1's episodes first, then disk
   2's continuing the count - and moved to
   "Season <NN>/<Title> (<Year>) S<NN>E<NN>.mkv". Detected extras go to
   "Season <NN>/extras/" (Jellyfin's generic-extras folder). Emptied "<S>-<D>"
   disk folders are removed after organizing.

   Season 0 ("0-<D>" disk folders) is treated as Jellyfin SPECIALS: every title
   is placed in "Season 00/" as "<Title> (<Year>) S00E<NN>.mkv", numbered in
   disk-then-title order. Extras detection is not applied to season 0 (a special
   is episode-like, with no reliable length signal to split it from an extra).

   TWO-PARTERS (stacked episodes): when a single file holds two or more
   consecutive episodes joined together, mark the source "title_<nn>x<k>.mkv"
   ("x<k>" = number of episodes in the file, e.g. "title_04x2.mkv"). It is then
   named "<Title> (<Year>) S<NN>E<AA>-E<BB>.mkv" (Jellyfin's stacked-episode
   form, mapping the one file to both slots) and the episode counter advances by
   <k>. This is an explicit marker on purpose: a joined two-parter is only ~2x a
   normal episode by length, which is too weak a signal to auto-stack (a wrong
   guess would misnumber the entire rest of the season). Duration is used only to
   WARN - an unusually long single title is flagged as a possible two-parter, and
   a marked title whose length disagrees with its part count is flagged too.

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
    --extra-threshold-pct of that median (default 70%).
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
  needs it.
- MOVIES: "main = longest" can't tell a real extra from a second cut
  (theatrical vs extended). If the two longest are within
  --similar-duration-pct, a warning is logged; the longest still wins.
- Only "title_<nn>.mkv" (optionally "title_<nn>x<k>.mkv") files in a disk folder
  are considered episodes/extras; any other .mkv there is left in place with a
  warning.
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
# A trailing metadata-provider id token, either the Plex/Sonarr/Radarr curly form
# ({imdb-tt..}, {tvdb-..}, {tmdb-..}) or Jellyfin's own square form
# ([imdbid-tt..], [tvdbid-..], [tmdbid-..]). Captured so it can be normalized to
# the square form Jellyfin actually reads.
TRAILING_ID_RE = re.compile(
    r"\s*(?:\{(?P<cprov>imdb|tvdb|tmdb)-(?P<cid>[A-Za-z0-9]+)\}"
    r"|\[(?P<sprov>imdbid|tvdbid|tmdbid)-(?P<sid>[A-Za-z0-9]+)\])\s*$",
    re.IGNORECASE,
)
# Matches a per-disc subfolder "<season>-<disk>", e.g. "1-1", "2-3", "10-1".
SEASON_DISK_RE = re.compile(r"^(?P<season>\d+)-(?P<disk>\d+)$")
# Matches a plain-numbered movie disc subfolder, e.g. "1", "2", "03". A
# multi-disc movie has these instead of loose .mkv files; the lowest number is
# the main-feature disc, higher numbers are extras discs.
MOVIE_DISC_RE = re.compile(r"^(?P<disc>\d+)$")
# Matches an already-organized season folder ("Season 00", "Season 01", ...),
# used only to give a re-run an accurate "already organized" skip reason.
SEASON_FOLDER_RE = re.compile(r"^Season \d+$", re.IGNORECASE)
# Matches an extracted title file "title_<nn>.mkv", with an optional "x<k>"
# multi-episode marker, e.g. "title_00.mkv" (one episode) or "title_04x2.mkv"
# (one file holding two consecutive episodes -> Jellyfin S..E..-E.. stacking).
EPISODE_FILE_RE = re.compile(r"^title_(?P<num>\d+)(?:x(?P<parts>\d+))?\.mkv$", re.IGNORECASE)
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


def _normalize_provider_id(prov: str, ident: str) -> str:
    """Return Jellyfin's square-bracket form, e.g. '[tvdbid-78581]'.

    Accepts either the curly key (imdb/tvdb/tmdb) or the square key
    (imdbid/tvdbid/tmdbid); Jellyfin only reads the square, id-suffixed form.
    """
    base = prov.lower()
    if base.endswith("id"):
        base = base[:-2]
    return f"[{base}id-{ident}]"


def parse_title_folder(folder_name: str) -> Optional[Tuple[str, str, Optional[str]]]:
    """Parse a "<Title> (<Year>) [id]" folder name.

    Returns (title, year, jellyfin_id or None), or None if it is not a
    title/year folder. A trailing provider id in either the Plex curly form or
    the Jellyfin square form is recognized and normalized to the square form.
    """
    name = folder_name.strip()
    jf_id: Optional[str] = None
    idm = TRAILING_ID_RE.search(name)
    if idm:
        prov = idm.group("cprov") or idm.group("sprov")
        ident = idm.group("cid") or idm.group("sid")
        jf_id = _normalize_provider_id(prov, ident)
        name = name[:idm.start()].rstrip()
    ty = TITLE_YEAR_RE.match(name)
    if not ty:
        return None
    return ty.group("title").strip(), ty.group("year"), jf_id


def desired_folder_name(title: str, year: str, jf_id: Optional[str]) -> str:
    base = sanitize_filename_component(f"{title} ({year})")
    return f"{base} {jf_id}" if jf_id else base


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
            self._fh = open(log_path, "w", encoding="utf-8")
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

def preflight_check_ffprobe() -> Optional[str]:
    if shutil.which("ffprobe") is None:
        return "ffprobe executable not found on PATH - install ffmpeg"
    return None


def probe_duration_seconds(path: Path) -> Optional[float]:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
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
#   ""        -> will be performed
#   "noop"    -> src is already at dest (already correctly named)
#   "exists"  -> a different, non-moving file already occupies dest (skip)
#   "dup"     -> another planned move also targets dest (skip)
#   "blocked" -> the move depends, through a rename chain, on an "exists"/"dup"
#                that never vacates, so it can never complete (skip)
# A move whose dest is occupied by ANOTHER file that is itself moving away is
# NOT a conflict: such chains and cycles are performed safely (reordering, and
# staging via a temporary name to break cycles) by execute_plan.
SKIP_STATUSES = ("exists", "dup", "blocked")


@dataclass(eq=False)  # identity-based, so moves can be graph nodes in sets/dicts
class PlannedMove:
    src: Path
    dest: Path
    kind: str                       # episode | extra | movie-main | movie-extra
    duration: Optional[float] = None
    ratio: Optional[float] = None   # duration / season median (TV only)
    flag: str = ""                  # "" | long | borderline | multipart-suspect
    parts: int = 1                  # episodes contained in this one file (>=2 = stacked)
    status: str = ""
    staged: bool = False            # part of a rename cycle -> resolved via a temp


@dataclass
class SeasonPlan:
    series_name: str
    series_root: Path
    season: int
    reference: Optional[float]
    detection_note: str
    moves: List[PlannedMove]


@dataclass
class MoviePlan:
    folder_name: str
    moves: List[PlannedMove]
    root: Optional[Path] = None   # movie folder, for relative source display


@dataclass(eq=False)
class FolderRename:
    src: Path
    dest: Path
    kind: str          # "series" | "movie"
    status: str = ""   # "" | noop | exists


@dataclass
class Plan:
    seasons: List[SeasonPlan] = field(default_factory=list)
    movies: List[MoviePlan] = field(default_factory=list)
    removals: List[Path] = field(default_factory=list)
    folder_renames: List[FolderRename] = field(default_factory=list)
    # recognized title folders that produced nothing: (folder, reason)
    skipped: List[Tuple[Path, str]] = field(default_factory=list)
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
    specials_placed: int = 0
    dirs_removed: int = 0
    folders_renamed: int = 0
    folders_skipped: int = 0
    conflicts: int = 0
    errors: int = 0
    warnings: int = 0


# --------------------------------------------------------------------------
# Discovery / classification
# --------------------------------------------------------------------------

def find_title_year_folders(input_root: Path) -> List[Path]:
    return sorted(
        p for p in input_root.rglob("*")
        if p.is_dir() and parse_title_folder(p.name) is not None
    )


def _plan_folder_rename(folder: Path, title: str, year: str, jf_id: Optional[str],
                        kind: str, plan: Plan) -> None:
    """Queue a rename of the series/movie folder to its Jellyfin-correct name."""
    target = folder.parent / desired_folder_name(title, year, jf_id)
    if target != folder:
        plan.folder_renames.append(FolderRename(folder, target, kind))


def folder_has_season_disks(folder: Path) -> bool:
    try:
        return any(p.is_dir() and SEASON_DISK_RE.match(p.name.strip()) for p in folder.iterdir())
    except OSError:
        return False


def folder_has_regular_season(folder: Path) -> bool:
    """True if the series has any non-zero season (season 0 == specials only)."""
    try:
        for p in folder.iterdir():
            if p.is_dir():
                sd = SEASON_DISK_RE.match(p.name.strip())
                if sd and int(sd.group("season")) != 0:
                    return True
    except OSError:
        pass
    return False


def folder_has_movie_discs(folder: Path) -> bool:
    """True if the folder holds plain-numbered disc subfolders (multi-disc movie)."""
    try:
        return any(p.is_dir() and MOVIE_DISC_RE.match(p.name.strip()) for p in folder.iterdir())
    except OSError:
        return False


def _collect_disk_titles(disk_path: Path, logger: Logger,
                         stats: Stats) -> Tuple[List[Tuple[int, int, Path]], List[Path]]:
    """Return (titles, leftovers). titles = [(title_number, parts, path)] sorted by number."""
    titles: List[Tuple[int, int, Path]] = []
    leftovers: List[Path] = []
    for f in sorted(disk_path.iterdir()):
        em = EPISODE_FILE_RE.match(f.name) if f.is_file() else None
        if em:
            parts = int(em.group("parts")) if em.group("parts") else 1
            if parts < 1:
                logger.warning(f"{disk_path}: '{f.name}' has a part count below 1 - treating as a single episode")
                stats.warnings += 1
                parts = 1
            titles.append((int(em.group("num")), parts, f))
        else:
            leftovers.append(f)
            if f.is_file() and f.suffix.lower() == ".mkv":
                logger.warning(f"{disk_path}: '{f.name}' does not match 'title_<nn>[x<k>].mkv' - "
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
    parts_of: Dict[Path, int] = {}
    for _disk_num, disk_path in disks:
        titles, leftovers = _collect_disk_titles(disk_path, logger, stats)
        plan.disk_info[disk_path] = (len(titles), bool(leftovers))
        for _n, parts, p in titles:
            ordered.append(p)
            parts_of[p] = parts
    if not ordered:
        return None

    durations: Dict[Path, Optional[float]] = {}
    ratios: Dict[Path, Optional[float]] = {p: None for p in ordered}
    flags: Dict[Path, str] = {p: "" for p in ordered}
    is_extra: Dict[Path, bool] = {p: False for p in ordered}
    reference: Optional[float] = None
    detection_note = ""
    specials = (season == 0)

    if specials:
        # Season 0 == Jellyfin specials. A special is episode-like, so there is
        # no reliable length signal to split it from an extra; every title is
        # kept as an S00Exx special and duration is not probed.
        detection_note = "specials - every title kept as S00Exx (extras detection not applied)"
    elif not args.detect_extras:
        detection_note = "extras detection off - every title treated as an episode"
    else:
        for p in ordered:
            durations[p] = probe_duration_seconds(p)
        # A K-part title covers K episodes, so its per-episode length is dur/K;
        # use that for the reference so a stacked title doesn't inflate the median.
        known = [d / parts_of[p] for p in ordered if (d := durations[p]) is not None and d > 0]
        if len(known) < EXTRA_DETECTION_MIN_TITLES:
            detection_note = (f"only {len(known)} probeable title(s) - too few to detect extras; "
                              f"all treated as episodes")
        else:
            reference = statistics.median(known)
            extra_ratio = args.extra_threshold_pct / 100.0
            for p in ordered:
                if parts_of[p] >= 2:
                    continue                       # a marked multi-parter is never an extra
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
                    if parts_of[p] >= 2:
                        # Advisory only: does the length agree with the marked count?
                        d = durations.get(p)
                        if d and d > 0:
                            ratios[p] = (d / parts_of[p]) / reference
                            if ratios[p] < extra_ratio or ratios[p] > LONG_EPISODE_RATIO:
                                flags[p] = "multipart-suspect"
                        continue
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
            moves.append(PlannedMove(p, dest, "extra", durations.get(p), ratios.get(p), flags.get(p, ""), parts=1))
        else:
            k = parts_of[p]
            start = episode_no + 1
            episode_no += k
            span = f"S{season:02d}E{start:02d}" if k == 1 else f"S{season:02d}E{start:02d}-E{episode_no:02d}"
            dest = season_folder / f"{series_stem} {span}.mkv"
            kind = "special" if specials else "episode"
            moves.append(PlannedMove(p, dest, kind, durations.get(p), ratios.get(p), flags.get(p, ""), parts=k))

    stats.seasons += 1
    return SeasonPlan(series_folder.name, series_folder, season, reference, detection_note, moves)


def plan_tv_series(series_folder: Path, args: argparse.Namespace, logger: Logger, stats: Stats, plan: Plan) -> None:
    parsed = parse_title_folder(series_folder.name)
    assert parsed is not None
    title, year, jf_id = parsed
    series_stem = sanitize_filename_component(f"{title} ({year})")  # episodes stay id-free

    seasons: Dict[int, List[Tuple[int, Path]]] = {}
    stray_mkvs: List[Path] = []
    stray_disc_dirs: List[Path] = []
    for p in series_folder.iterdir():
        if p.is_dir():
            sd = SEASON_DISK_RE.match(p.name.strip())
            if sd:
                seasons.setdefault(int(sd.group("season")), []).append((int(sd.group("disk")), p))
            elif MOVIE_DISC_RE.match(p.name.strip()):
                stray_disc_dirs.append(p)
        elif p.is_file() and p.suffix.lower() == ".mkv":
            stray_mkvs.append(p)

    if not seasons:
        return
    if stray_mkvs:
        logger.warning(f"{series_folder}: {len(stray_mkvs)} .mkv file(s) sit directly in the series folder "
                       f"(outside any '<S>-<D>' disk folder) - left untouched")
        stats.warnings += 1
    if stray_disc_dirs:
        names = ", ".join(sorted(d.name for d in stray_disc_dirs))
        logger.warning(f"{series_folder}: also has plain-numbered subfolder(s) ({names}), the movie-disc form - "
                       f"treating this folder as a TV series because it has season-disk '<S>-<D>' folders, and "
                       f"IGNORING the plain-numbered ones. If this is actually a movie, drop the '<S>-<D>' "
                       f"folders; if it's TV, rename these to '<season>-<disk>'. Check the naming")
        stats.warnings += 1

    produced = False
    for season in sorted(seasons):
        disks = sorted(seasons[season], key=lambda item: item[0])
        sp = _plan_season(series_folder, series_stem, season, disks, args, logger, stats, plan)
        if sp is not None:
            plan.seasons.append(sp)
            produced = True

    if not produced:
        # Season-disk folders exist but hold no episodes: nothing to organize,
        # so leave the folder completely alone (no re-tag either).
        plan.skipped.append((series_folder, "TV series: season-disk folders contain no title_<nn>.mkv files"))
        return
    stats.series_folders += 1
    # The id (if any) belongs on the series folder, so re-tag it to Jellyfin form.
    _plan_folder_rename(series_folder, title, year, jf_id, "series", plan)


# --------------------------------------------------------------------------
# Planning: movies
# --------------------------------------------------------------------------

def _warn_similar_cuts(folder: Path, probed: List[Tuple[Path, Optional[float]]],
                       args: argparse.Namespace, logger: Logger, stats: Stats) -> None:
    """Warn if the two longest titles are close in length (possible second cut)."""
    if len(probed) >= 2:
        longest, second = probed[0][1] or 0.0, probed[1][1] or 0.0
        if longest > 0 and (longest - second) / longest * 100.0 < args.similar_duration_pct:
            logger.warning(f"{folder}: the two longest files are within {args.similar_duration_pct:g}% of each "
                           f"other ({probed[0][0].name}: {format_duration(longest)} vs {probed[1][0].name}: "
                           f"{format_duration(second)}) - picking the longest as the main feature, but this may "
                           f"be two cuts of the movie (theatrical vs extended); please double check")
            stats.warnings += 1


def plan_movie_folder(folder: Path, args: argparse.Namespace, logger: Logger, stats: Stats,
                      plan: Plan) -> Optional[MoviePlan]:
    parsed = parse_title_folder(folder.name)
    assert parsed is not None
    title, year, jf_id = parsed
    canonical_stem = sanitize_filename_component(f"{title} ({year})")
    # For movies the id goes on the main file too (it must match the folder name).
    main_stem = f"{canonical_stem} {jf_id}" if jf_id else canonical_stem

    mkvs = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".mkv")
    if not mkvs:
        if any(p.is_dir() and SEASON_FOLDER_RE.match(p.name) for p in folder.iterdir()):
            plan.skipped.append((folder, "already organized (contains Season folders)"))
        else:
            plan.skipped.append((folder, "no .mkv files directly in the folder"))
        return None
    stats.movie_folders += 1

    probed: List[Tuple[Path, Optional[float]]] = []
    for f in mkvs:
        d = probe_duration_seconds(f)
        if d is None:
            logger.warning(f"{folder}: could not determine duration of {f.name} (ffprobe failed) - treating as 0s")
            stats.warnings += 1
        probed.append((f, d))
    probed.sort(key=lambda item: (item[1] or 0.0), reverse=True)
    _warn_similar_cuts(folder, probed, args, logger, stats)

    moves: List[PlannedMove] = []
    main_file, main_dur = probed[0]
    moves.append(PlannedMove(main_file, folder / f"{main_stem}.mkv", "movie-main", main_dur))
    extras_folder = folder / "extras"
    for n, (f, d) in enumerate(probed[1:], start=1):
        dest = extras_folder / f"{canonical_stem} - Extra {n:02d}.mkv"
        moves.append(PlannedMove(f, dest, "movie-extra", d))

    _plan_folder_rename(folder, title, year, jf_id, "movie", plan)
    return MoviePlan(folder.name, moves, folder)


def plan_multidisc_movie(folder: Path, args: argparse.Namespace, logger: Logger, stats: Stats,
                         plan: Plan) -> Optional[MoviePlan]:
    """Multi-disc movie: lowest disc = single-disc-style main+extras, rest all extras."""
    parsed = parse_title_folder(folder.name)
    assert parsed is not None
    title, year, jf_id = parsed
    canonical_stem = sanitize_filename_component(f"{title} ({year})")
    main_stem = f"{canonical_stem} {jf_id}" if jf_id else canonical_stem
    extras_folder = folder / "extras"

    disc_dirs: List[Tuple[int, Path]] = []
    stray_mkvs: List[Path] = []
    for p in folder.iterdir():
        if p.is_dir() and MOVIE_DISC_RE.match(p.name.strip()):
            disc_dirs.append((int(p.name.strip()), p))
        elif p.is_file() and p.suffix.lower() == ".mkv":
            stray_mkvs.append(p)
    disc_dirs.sort(key=lambda item: item[0])
    if not disc_dirs:
        return None

    main_num, main_disc = disc_dirs[0]
    main_titles, main_leftovers = _collect_disk_titles(main_disc, logger, stats)
    if not main_titles:
        plan.skipped.append((folder, f"multi-disc movie: main disc '{main_disc.name}' has no title_<nn>.mkv files"))
        return None

    stats.movie_folders += 1
    if stray_mkvs:
        logger.warning(f"{folder}: {len(stray_mkvs)} .mkv file(s) sit directly in the movie folder "
                       f"(outside any disc subfolder) - left untouched")
        stats.warnings += 1
    plan.disk_info[main_disc] = (len(main_titles), bool(main_leftovers))

    # Main disc: longest title is the main feature, exactly like a single-disc movie.
    dur_of = {p: probe_duration_seconds(p) for _n, _pp, p in main_titles}
    probed = sorted(((p, dur_of[p]) for _n, _pp, p in main_titles),
                    key=lambda item: (item[1] or 0.0), reverse=True)
    _warn_similar_cuts(folder, probed, args, logger, stats)
    main_file, main_dur = probed[0]

    moves: List[PlannedMove] = [PlannedMove(main_file, folder / f"{main_stem}.mkv", "movie-main", main_dur)]
    extra_no = 0
    # Remaining main-disc titles (title order) -> extras.
    for _n, _pp, p in main_titles:
        if p == main_file:
            continue
        extra_no += 1
        moves.append(PlannedMove(p, extras_folder / f"{canonical_stem} - Extra {extra_no:02d}.mkv",
                                 "movie-extra", dur_of[p]))
    # Every title on the higher discs -> extras (disc order, then title order).
    for _disc_num, disc in disc_dirs[1:]:
        titles, leftovers = _collect_disk_titles(disc, logger, stats)
        plan.disk_info[disc] = (len(titles), bool(leftovers))
        for _n, _pp, p in titles:
            extra_no += 1
            moves.append(PlannedMove(p, extras_folder / f"{canonical_stem} - Extra {extra_no:02d}.mkv",
                                     "movie-extra", None))

    _plan_folder_rename(folder, title, year, jf_id, "movie", plan)
    return MoviePlan(folder.name, moves, folder)


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
        elif m.dest.exists() and m.dest not in srcs:
            # Occupied by a file that is NOT itself moving away -> real conflict.
            m.status = "exists"
        else:
            # Free, or occupied by another file that IS moving away (a chain or
            # cycle). resolve_chains() decides which of these can actually run.
            m.status = ""


def resolve_chains(plan: Plan) -> None:
    """Classify the entangled ("") moves.

    A move whose target is occupied by another moving file is fine *if* that
    file eventually vacates. Following the "who sits on my target" links, a move
    is:
      - blocked : the chain ends at an "exists"/"dup" that never moves;
      - staged  : the chain loops back on itself (a cycle needs a temp file);
      - plain    : the chain reaches a free slot (just needs the right order).
    Only blocked moves are skipped; the rest are performed by execute_plan.
    """
    moves = plan.all_moves()
    # occupant[path] = the move whose source currently sits at that path.
    # Includes skipped moves, whose files stay put and keep blocking.
    occupant = {m.src: m for m in moves if m.status != "noop"}

    for m in moves:
        if m.status != "":
            continue
        seen = {m}
        cur = m
        while True:
            nxt = occupant.get(cur.dest)      # who occupies cur's target?
            if nxt is None:                   # target is a free slot
                break
            if nxt.status in ("exists", "dup", "blocked"):
                m.status = "blocked"          # chain dead-ends at a real conflict
                break
            if nxt in seen:                   # looped back -> resolvable cycle
                break
            seen.add(nxt)
            cur = nxt

    # Flag cycle members (target occupied by another *performable* move, and the
    # chain returns to itself) so the preview can note the staging.
    performable = {m.src: m for m in moves if m.status == ""}
    for m in moves:
        if m.status != "":
            continue
        seen = {m}
        cur = performable.get(m.dest)
        while cur is not None:
            if cur is m:
                m.staged = True
                break
            if cur in seen:
                break
            seen.add(cur)
            cur = performable.get(cur.dest)


def resolve_removals(plan: Plan) -> None:
    moves_by_disk: Dict[Path, List[PlannedMove]] = defaultdict(list)
    for mv in plan.all_moves():
        moves_by_disk[mv.src.parent].append(mv)
    removable: List[Path] = []
    for disk, (n_titles, had_leftovers) in plan.disk_info.items():
        if n_titles > 0 and not had_leftovers and all(mv.status == "" for mv in moves_by_disk.get(disk, [])):
            removable.append(disk)
    plan.removals = sorted(removable)


def resolve_folder_renames(plan: Plan) -> None:
    for fr in plan.folder_renames:
        if fr.src == fr.dest:
            fr.status = "noop"
        elif fr.dest.exists():
            fr.status = "exists"
        else:
            fr.status = ""


# --------------------------------------------------------------------------
# Rendering (--dry-run)
# --------------------------------------------------------------------------

def _target_rel(move: PlannedMove) -> str:
    if move.kind in ("extra", "movie-extra"):
        return f"extras/{move.dest.name}"
    return move.dest.name


def _tag_for(move: PlannedMove) -> str:
    if move.status == "noop":
        return "  (already named - no change)"
    if move.status == "exists":
        return "  [SKIP: a different file already exists at the target]"
    if move.status == "dup":
        return "  [SKIP: two files would land on this same target]"
    if move.status == "blocked":
        return "  [SKIP: blocked by a conflict upstream in a rename chain]"
    if move.kind == "extra":
        base = "  [EXTRA]"
    elif move.kind == "special":
        base = "  [SPECIAL]"
    elif move.kind == "movie-main":
        base = "  [main feature]"
    else:
        base = ""
    if move.staged:
        base += "  [rename cycle - resolved via a temporary file]"
    if move.parts >= 2:
        base += f"  [{move.parts}-parter -> stacked]"
    if move.flag == "long":
        base += "  [unusually long - if it's a joined two-parter, mark it title_NNx2]"
    elif move.flag == "borderline":
        base += "  [borderline call - verify]"
    elif move.flag == "multipart-suspect":
        base += "  [length looks off for this part count - verify]"
    return base


def _leaf_annotation(move: PlannedMove, src_disp: str) -> str:
    """Right-hand annotation for a file in the result tree: source, length, tags."""
    if move.status == "noop":
        return "  (already named - unchanged)"
    parts = [f"<- {src_disp}"]
    if move.duration is not None:
        parts.append(f"({format_duration(move.duration)})")
    if move.ratio is not None:
        parts.append(f"{move.ratio * 100:.0f}% of typical")
    tag = _tag_for(move).strip()
    if tag:
        parts.append(tag)
    return "  " + "  ".join(parts)


def _print_result_tree(title_line: str, sub_note: Optional[str],
                       leaves: List[Tuple[Tuple[str, ...], str]],
                       removed: List[str], R) -> None:
    """Print one title folder as an indented final-state tree.

    leaves: (relative-path-parts, annotation). removed: disk folder names.
    """
    R("")
    R(title_line)
    if sub_note:
        R(f"    {sub_note}")

    # Build a nested {dirs, files} tree from the destination relative paths.
    root: Dict = {"dirs": {}, "files": []}
    for parts, annot in leaves:
        node = root
        for d in parts[:-1]:
            node = node["dirs"].setdefault(d, {"dirs": {}, "files": []})
        node["files"].append((parts[-1], annot))

    def walk(node: Dict, indent: str) -> None:
        fwidth = max((len(name) for name, _ in node["files"]), default=0)
        for name, annot in node["files"]:
            R(f"{indent}{name:<{fwidth}}{annot}")
        for dname in sorted(node["dirs"]):
            R(f"{indent}{dname}/")
            walk(node["dirs"][dname], indent + "    ")

    walk(root, "    ")
    if removed:
        R(f"    (empty disk folder(s) removed: {', '.join(removed)})")


def render_plan(plan: Plan, logger: Logger, input_root: Path) -> None:
    R = logger.report
    moves = plan.all_moves()
    to_do = [m for m in moves if m.status == ""]
    conflicts = [m for m in moves if m.status in SKIP_STATUSES]
    renames = [fr for fr in plan.folder_renames if fr.status != "noop"]

    # Final folder name per title folder (root -> displayed name after rename).
    rename_by_root: Dict[Path, FolderRename] = {fr.src: fr for fr in plan.folder_renames}
    removals_by_root: Dict[Path, List[str]] = defaultdict(list)
    for d in plan.removals:
        removals_by_root[d.parent].append(d.name)

    R()
    R("=================  DRY RUN  =================")
    R(f"Root: {input_root}")
    R("Nothing below has happened yet - this is exactly what a real run would do.")

    def render_skipped() -> None:
        if not plan.skipped:
            return
        R("")
        R("Skipped (recognized folders with nothing to organize - left untouched):")
        def disp(f: Path) -> str:
            try:
                return str(f.relative_to(input_root))
            except ValueError:
                return f.name
        width = max(len(disp(f)) for f, _ in plan.skipped)
        for f, reason in sorted(plan.skipped):
            R(f"    {disp(f):<{width}}  - {reason}")

    if not moves and not renames and not plan.skipped:
        R("")
        R("No '<Title> (<Year>)' folders with anything to organize were found.")
        R("=============================================")
        return
    if not to_do and not conflicts and not plan.removals and not renames:
        R("")
        R("Nothing to organize.")
        render_skipped()
        R("")
        R("=============================================")
        return

    # ---- Up-front manifest of the scope ----
    R("")
    R(f"Planned changes:  {len(to_do)} file(s) moved/renamed | "
      f"{len(renames)} title folder(s) re-tagged | "
      f"{len(plan.removals)} empty disk folder(s) removed | "
      f"{len(conflicts)} conflict(s) skipped | "
      f"{len(plan.skipped)} folder(s) skipped")
    R("Each file below shows its FINAL name and, after '<-', where it comes from.")

    def header(root: Path, kind: str) -> Tuple[str, Optional[str]]:
        fr = rename_by_root.get(root)
        if fr is not None and fr.status == "":
            return f"{kind}  {fr.dest.name}/", f"(folder renamed from \"{root.name}\")"
        if fr is not None and fr.status == "exists":
            return (f"{kind}  {root.name}/",
                    f"(would re-tag to \"{fr.dest.name}\" but that folder already exists - SKIPPED)")
        return f"{kind}  {root.name}/", None

    def leaves_for(mvs: List[PlannedMove], root: Path) -> List[Tuple[Tuple[str, ...], str]]:
        out = []
        for m in mvs:
            if m.status in SKIP_STATUSES:
                continue  # skipped -> won't appear in the result; listed under Conflicts
            rel_parts = m.dest.relative_to(root).parts
            try:
                src_disp = str(m.src.relative_to(root))
            except ValueError:
                src_disp = m.src.name
            out.append((rel_parts, _leaf_annotation(m, src_disp)))
        return out

    # ---- TV series, grouped by folder ----
    seasons_by_root: Dict[Path, List[SeasonPlan]] = defaultdict(list)
    for sp in plan.seasons:
        seasons_by_root[sp.series_root].append(sp)

    for root, sps in seasons_by_root.items():
        title_line, sub = header(root, "[TV]")
        leaves: List[Tuple[Tuple[str, ...], str]] = []
        notes = []
        for sp in sorted(sps, key=lambda s: s.season):
            leaves.extend(leaves_for(sp.moves, root))
            notes.append(f"Season {sp.season:02d}: {sp.detection_note}")
        _print_result_tree(title_line, sub, leaves, removals_by_root.get(root, []), R)
        for n in notes:
            R(f"    - {n}")

    # ---- Movies (single- and multi-disc), one folder each ----
    for mp in plan.movies:
        root = mp.root if mp.root is not None else (mp.moves[0].src.parent if mp.moves else input_root)
        title_line, sub = header(root, "[Movie]")
        leaves = leaves_for(mp.moves, root)
        _print_result_tree(title_line, sub, leaves, removals_by_root.get(root, []), R)

    # ---- Recognized folders that were skipped ----
    render_skipped()

    # ---- Conflicts, called out together (these do NOT happen) ----
    if conflicts:
        R("")
        R(f"!! {len(conflicts)} conflict(s) SKIPPED - nothing is overwritten:")
        for m in conflicts:
            R(f"    {m.src}")
            R(f"      -> {m.dest}{_tag_for(m)}")

    R("")
    R("=============================================")


# --------------------------------------------------------------------------
# Execution (real run)
# --------------------------------------------------------------------------

def _dest_display(src: Path, dest: Path) -> str:
    return dest.name if dest.parent == src.parent else f"{dest.parent.name}/{dest.name}"


def _unique_staging_path(directory: Path, suffix: str, used: set) -> Path:
    n = 0
    while True:
        cand = directory / f".organize_media.staging.{n}{suffix or '.tmp'}"
        if cand not in used and not cand.exists():
            used.add(cand)
            return cand
        n += 1


def _perform_move(m: PlannedMove, origin: Path, logger: Logger, stats: Stats) -> bool:
    """Move m.src -> m.dest. `origin` is the file's ORIGINAL location, for logs."""
    shown = _dest_display(origin, m.dest)
    try:
        m.dest.parent.mkdir(parents=True, exist_ok=True)
        m.src.replace(m.dest)
    except OSError as e:
        logger.error(f"Failed to rename {origin} -> {shown}: {e}")
        stats.errors += 1
        return False
    logger.info(f"Renamed {origin} -> {shown}")
    stats.files_renamed += 1
    if m.kind in ("extra", "movie-extra"):
        stats.extras_separated += 1
    elif m.kind == "special":
        stats.specials_placed += 1
    if m.flag == "long":
        logger.warning(f"{shown}: kept as a single episode but unusually long "
                       f"({format_duration(m.duration)}) - if it's a joined two-parter, "
                       f"mark the source title_NNx2 and re-run")
        stats.warnings += 1
    elif m.flag == "borderline":
        logger.warning(f"{shown}: borderline episode/extra call "
                       f"({format_duration(m.duration)}) - verify")
        stats.warnings += 1
    elif m.flag == "multipart-suspect":
        logger.warning(f"{shown}: marked as a {m.parts}-part file but its length "
                       f"({format_duration(m.duration)}) looks off for that many episodes - "
                       f"verify the part count")
        stats.warnings += 1
    return True


def execute_plan(plan: Plan, logger: Logger, stats: Stats) -> None:
    moves = plan.all_moves()

    # No-ops and unresolvable conflicts: account and report, don't touch disk.
    for m in moves:
        if m.status == "noop":
            stats.files_already_correct += 1
        elif m.status in SKIP_STATUSES:
            reason = {
                "exists": "a different file already exists at the target",
                "dup": "two files target the same name",
                "blocked": "blocked by a conflict upstream in a rename chain",
            }[m.status]
            logger.error(f"Skipping {m.src.name} -> {_dest_display(m.src, m.dest)}: {reason}")
            stats.conflicts += 1

    # Perform the rest in a dependency-safe order: a move runs once its target is
    # no longer occupied by another still-pending source. Chains just need the
    # right order; a cycle (moves remain but none is ready) is broken by staging
    # one file to a temporary name. `origin` remembers each file's true starting
    # point so logs read naturally even after a stage.
    performable = [m for m in moves if m.status == ""]
    origin = {m: m.src for m in performable}
    occupied = {m.src for m in performable}
    used_temps: set = set()
    remaining = list(performable)
    guard = 4 * len(performable) + 16

    while remaining and guard > 0:
        guard -= 1
        progressed = False
        for m in list(remaining):
            if m.dest in occupied:
                continue                    # target still holds a pending file
            occupied.discard(m.src)
            _perform_move(m, origin[m], logger, stats)  # errors are logged inside
            remaining.remove(m)
            progressed = True
        if progressed or not remaining:
            continue
        # Stalled -> everything left is on a cycle. Stage one file aside.
        m = remaining[0]
        temp = _unique_staging_path(m.src.parent, m.src.suffix, used_temps)
        try:
            occupied.discard(m.src)
            m.src.replace(temp)
        except OSError as e:
            logger.error(f"Failed to stage {origin[m].name} to resolve a rename cycle: {e}")
            stats.errors += 1
            remaining.remove(m)
            continue
        logger.info(f"Staged {origin[m].name} to a temporary name to resolve a rename cycle")
        m.src = temp                        # its final move is now temp -> dest

    if remaining:  # should not happen; safety valve against an unforeseen loop
        for m in remaining:
            logger.error(f"Could not complete move of {origin[m].name} -> {m.dest.name} "
                         f"(unresolved dependency) - left in place")
            stats.errors += 1

    for disk in plan.removals:
        try:
            disk.rmdir()
            logger.info(f"Removed empty disk folder {disk}")
            stats.dirs_removed += 1
        except OSError as e:
            logger.warning(f"Could not remove disk folder {disk}: {e}")
            stats.warnings += 1

    # Folder renames last: everything inside has already been organized, so the
    # top folder (and all its new contents) just gets its Jellyfin-correct name.
    for fr in plan.folder_renames:
        if fr.status == "noop":
            continue
        if fr.status == "exists":
            logger.error(f"Skipping folder rename {fr.src.name} -> {fr.dest.name}: "
                         f"a different folder already exists at that name")
            stats.conflicts += 1
            continue
        try:
            fr.src.replace(fr.dest)
            logger.info(f"Renamed {fr.kind} folder {fr.src.name} -> {fr.dest.name}")
            stats.folders_renamed += 1
        except OSError as e:
            logger.error(f"Failed to rename folder {fr.src.name} -> {fr.dest.name}: {e}")
            stats.errors += 1


def tally_plan_into_stats(plan: Plan, stats: Stats) -> None:
    """For --dry-run: make the summary reflect the plan without executing it."""
    for m in plan.all_moves():
        if m.status == "noop":
            stats.files_already_correct += 1
        elif m.status in SKIP_STATUSES:
            stats.conflicts += 1
        else:
            stats.files_renamed += 1
            if m.kind in ("extra", "movie-extra"):
                stats.extras_separated += 1
            elif m.kind == "special":
                stats.specials_placed += 1
    stats.dirs_removed += len(plan.removals)
    for fr in plan.folder_renames:
        if fr.status == "exists":
            stats.conflicts += 1
        elif fr.status != "noop":
            stats.folders_renamed += 1


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
    p.add_argument("-l", "--log", nargs="?", const=None, default=None, metavar="LOGFILE",
                   help="Log file path (default: organize.log in the --input folder)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print exactly what a real run would do, changing nothing")
    p.add_argument("--no-detect-extras", dest="detect_extras", action="store_false",
                   help="TV: don't use running time to separate 'extras' from episodes (TV then needs no ffprobe)")
    p.add_argument("--extra-threshold-pct", type=float, default=70.0, metavar="PCT",
                   help="TV: a title shorter than this %% of the season's median episode length is an extra")
    p.add_argument("--similar-duration-pct", type=float, default=10.0, metavar="PCT",
                   help="MOVIES: warn if the two longest files are within this %% of each other in duration")
    p.set_defaults(detect_extras=True)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    input_root = Path(args.input).resolve()
    log_path = Path(args.log).resolve() if args.log else input_root / DEFAULT_LOG
    logger = Logger(log_path)

    if not input_root.is_dir():
        logger.error(f"Input folder does not exist: {input_root}")
        logger.close()
        return 1

    folders = find_title_year_folders(input_root)
    tv_series = [f for f in folders if folder_has_season_disks(f)]
    multidisc = [f for f in folders if f not in tv_series and folder_has_movie_discs(f)]
    movies = [f for f in folders if f not in tv_series and f not in multidisc]
    logger.info(f"Found {len(folders)} '<Title> (<Year>)' folder(s) under {input_root} "
                f"({len(tv_series)} TV series, {len(multidisc)} multi-disc movie, "
                f"{len(movies)} single-disc movie)")

    need_ffprobe = bool(movies) or bool(multidisc) or (
        args.detect_extras and any(folder_has_regular_season(f) for f in tv_series)
    )
    if need_ffprobe:
        err = preflight_check_ffprobe()
        if err:
            logger.error(err)
            logger.error("Aborting before touching any files - install ffmpeg/ffprobe "
                         "or (TV only) pass --no-detect-extras")
            logger.close()
            return 1

    stats = Stats()

    # --- Phase 1: plan everything (no disk changes) ---
    plan = Plan()
    for folder in tv_series:
        plan_tv_series(folder, args, logger, stats, plan)
    for folder in multidisc:
        mp = plan_multidisc_movie(folder, args, logger, stats, plan)
        if mp is not None:
            plan.movies.append(mp)
    for folder in movies:
        mp = plan_movie_folder(folder, args, logger, stats, plan)
        if mp is not None:
            plan.movies.append(mp)
    resolve_statuses(plan)
    resolve_chains(plan)
    resolve_removals(plan)
    resolve_folder_renames(plan)

    for folder, reason in plan.skipped:
        logger.info(f"Skipped: {folder}  ({reason})")
    stats.folders_skipped = len(plan.skipped)

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
        row("Extras placed (TV + movie)", stats.extras_separated),
        row("TV specials placed", stats.specials_placed),
        row(remove_label, stats.dirs_removed),
        row("Title folders re-tagged", stats.folders_renamed),
        row("Folders skipped (nothing to do)", stats.folders_skipped),
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
