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

2. Playlist-obfuscation ("ScreenPass" / UOPs) handling uses THREE signals,
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
        named "main_title.mkv", so other tools working on the output folder
        afterward (organize_media.py, a HandBrake batch script, etc.) can
        trust the filename rather than re-deriving which title was the main
        feature themselves. Every other extracted title is named
        "title_NN.mkv", where NN is its MakeMKV title number - a stable,
        sortable, standardized name (important for TV discs, where each
        title is an episode) rather than MakeMKV's disc-label-derived
        default.

     b) A duration-clustering fallback, used only when no unambiguous
        FPL_MainFeature marker is found. This looks for a large cluster
        of titles that all share (almost) the same duration - the classic
        signature of ScreenPass/UOPs discs presenting many decoy
        playlists. Rather than exact round-to-second bucketing, titles are
        clustered with a sliding tolerance window
        (--obfuscation-tolerance-sec, default 2s), because decoys are
        frequently only *similar* in length, not frame-identical, and
        exact rounding would split one logical cluster across adjacent
        buckets and undercount it. The threshold defaults to 30 titles
        sharing a duration (--obfuscation-threshold): a very low threshold
        like 5 is NOT used, since perfectly ordinary discs (TV box sets,
        multi-angle features, bonus loops) can have a handful of titles at
        the same length with no obfuscation at all; but 30 sits well above
        that normal clustering while still catching the dozens-scale
        obfuscation that real ScreenPass/Lionsgate discs (especially DVDs,
        where the BD-Java FPL_MainFeature signal isn't available at all)
        commonly use - reported counts range from dozens to hundreds of
        same/similar-length decoys.
        When this fallback fires, and after filtering by --min-length
        there is exactly ONE title left that could be the main feature,
        that title is extracted. If more than one plausible candidate
        remains, we cannot safely guess: the disc is treated as obfuscated
        and NOT converted (see the shared handling under (c) below).

     c) A size-based obfuscation signal, checked after the candidate set is
        finalized (post play-all removal and duplicate dedup) and applied to
        every automatic path (not just the duration-cluster one). If the
        selected titles' combined estimated size exceeds the source ISO's
        own size by more than a large factor (--obfuscation-max-size-ratio,
        default 3x), that many distinct titles physically cannot fit on the
        disc, so they must be decoy playlists over the same content. This is
        what catches discs (e.g. "You're Next") that deliberately spread
        their decoy runtimes across minutes so no tight duration cluster
        forms under (b) - the physical size math gives them away regardless.
        It's guarded by a minimum candidate count (so a couple of genuinely
        overlapping alternate cuts can't trip it) and runs BEFORE any
        extraction, so an obfuscated disc is caught up front rather than
        after grinding out a runaway pile of fake titles.

        Shared handling for (b)-ambiguous and (c): in a normal run this is a
        fail-fast error - the reason is logged (with guidance to research
        the correct playlist and re-run with --main-playlist, or exclude the
        disc) and the run exits, rather than converting a disc's worth of
        decoys. In --dry-run it is downgraded to a warning and the disc is
        listed as needing attention, so a survey run reports every problem
        disc instead of stopping at the first.

     d) Manual override (--main-playlist), for the case where the signals
        above fail - i.e. MakeMKV (even with a working JRE) can't identify
        the main title, so the disc would otherwise be rejected by (b)/(c).
        If you research the correct playlist for your specific disc
        (community forums post these per release) you can pass its source
        playlist filename, e.g. --main-playlist 00610.mpls. This bypasses
        all automatic detection for that disc (including the (c) size guard)
        and forces that title as the main feature (named main_title.mkv),
        additionally keeping every title clearly SHORTER than it as extras
        (deleted scenes, featurettes) while excluding the same-length
        decoys. Matching is on the source PLAYLIST name - reported by
        MakeMKV as each title's "Source file name" (attribute 16) - and NOT
        on MakeMKV's title index, which isn't stable across versions or
        --min-length settings. Because a playlist name only makes sense for
        one specific disc, the run must resolve to exactly one ISO - either
        point --input at a folder containing a single ISO, or narrow a
        larger folder with --include/--exclude; the script aborts up front
        otherwise.

   In both cases, makemkvcon is NEVER invoked with "all" - only explicit
   title numbers are ever passed.

   Signals (a) and (b) are Blu-ray specific (BD-Java doesn't exist on DVD,
   and DVD protection schemes don't produce hundreds of decoy titles), so
   this script first classifies each ISO as DVD or Blu-ray by FILE SIZE
   and skips those for anything classified as DVD - see point 2a-DVD below.
   The size-based signal (c), by contrast, applies to every automatic path
   including DVD, since its physical-size reasoning holds regardless of disc
   type.

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
   --no-detect-playall if a disc's real structure ever fights with
   this.

Test this script with --dry-run against your library before turning it
loose, and consider trying it on a copy of a known ScreenPass disc (e.g.
John Wick) to confirm the obfuscation heuristic behaves the way you want.

7. Safety/workflow additions on top of the above:
     - Before deleting a source ISO, the script verifies a real (non-tiny)
       .mkv file actually appeared/changed in the output directory for
       each extracted title - a 0-exit-code from makemkvcon is treated as
       necessary but not sufficient. See MIN_OUTPUT_FILE_BYTES.
     - A pre-flight check confirms makemkvcon64.exe (the 64-bit MakeMKV
       CLI, required deliberately over the memory-limited 32-bit build)
       can be found on PATH before any files are touched.
     - Fail-fast: the run is NOT generally resilient to per-disc failures.
       The moment an error is logged - a failed title-info scan, a failed
       title extraction, insufficient free space, an unexpected
       exception, etc. - the error is written to the log and console as
       usual and the whole run stops immediately (nonzero exit). This
       lives in one place (DualLogger.error()) so it applies uniformly
       everywhere an error is logged, current or future.
       The ONE deliberate exception is the stall timeout (below), which
       uses DualLogger.error_continue(): a hung disc is known to be
       isolated to that ISO, so it's logged as an error, that ISO is
       abandoned with its source retained, and the batch continues. The
       run still exits nonzero.
     - Free space on the output volume is checked before each title
       extraction (using MakeMKV's own reported title size plus
       --free-space-margin-pct headroom), so a nearly-full output drive
       fails fast on one title instead of partway through a multi-hour
       extraction.
     - Resume support: after a fully successful conversion, a small
       manifest file (.iso_to_mkv_manifest.json) is written into the
       ISO's output folder, recording the source ISO's size/mtime, the
       exact set of title IDs extracted, the output filenames, and
       every argument that could affect title selection (--min-length,
       --disc-type, --obfuscation-threshold, the play-all settings,
       etc.). On a later run, an ISO is skipped as "already converted"
       only if that manifest exists AND matches this exact ISO, this
       exact candidate-title selection, and these exact settings, AND
       every recorded output file is still present and still looks like
       real output. The source-ISO match is size plus mtime (never a
       content hash - hashing a multi-GB image every run would be far too
       slow), and the mtime comparison allows a couple of seconds of
       slack rather than requiring bit-exact equality, so filesystem
       timestamp jitter (exFAT/FAT's 2s granularity, network-share or
       backup/restore rounding) can't spuriously force a huge disc to be
       re-converted while still catching a genuinely replaced source. When
       a manifest exists but doesn't match, the specific reason is logged
       (size changed, mtime changed, title set changed, a setting changed,
       an output file missing) so an unexpected re-conversion is easy to
       diagnose. Note --limit is deliberately NOT part of the match, so
       the common "convert up to a byte budget, then re-run to continue"
       workflow reliably skips everything already done. This is
       deliberately stricter than just counting .mkv files in the folder:
       a folder that happens to hold the "right number" of files from a
       *different* configuration (a looser --min-length, a forced
       --disc-type, etc.) is no longer mistaken for a completed run of the
       current one - it's logged and redone instead. This is whole-ISO
       granularity only - an interrupted multi-title disc is safely redone
       in full rather than partially resumed. Force a redo with
       -f/--force.
     - Idempotent re-extraction: whenever an ISO is (re)extracted rather
       than resume-skipped (no matching manifest, or --force), the output
       folder is first cleared of that ISO's previous output (its .mkv
       files and the resume manifest) so the conversion starts from a
       clean slate. makemkvcon writes each title under its own
       disc-label-derived name (e.g. MovieTitle_t00.mkv) which this script
       then renames to the standardized title_NN.mkv / main_title.mkv;
       without the pre-clear, re-extracting into a folder that still held
       the previous run's title_NN.mkv hit the rename's collision guard
       and left MakeMKV's raw name in place, so identical input could
       yield title_NN.mkv on one run and MovieTitle_t00.mkv on the next
       (plus an orphaned stale file). Clearing first makes output naming
       deterministic. Only this script's own artifacts are removed;
       anything else in the folder (hand-added cover art, external
       subtitles, etc.) is left untouched.
     - Duplicate-main-title dedup: some Blu-rays expose the main feature
       as several titles whose playlists all reference the identical
       source segments (redundant or seamless-branching playlists, or a
       mild anti-ripping tactic). When no single (FPL_MainFeature) marker
       singles one out, the fallback selection would otherwise keep every
       title over --min-length and extract all the copies - wasting space
       on identical output and producing a combined size that can exceed
       the source ISO (tripping the runaway-output failsafe below).
       Instead, titles are compared by SEGMENT MAP (MakeMKV attribute 26,
       the ordered list of source segments a playlist references): two
       titles are duplicates only when their segment maps are identical,
       and one copy (the lowest title id, for deterministic naming) is
       kept. This is deliberately NOT duration/size similarity - distinct
       TV episodes on one disc routinely share a runtime and a size to a
       fraction of a percent, so any duration/size tolerance loose enough
       to catch real duplicates would also merge genuinely different
       episodes; the segment map is a content fingerprint that separates
       "same content, multiple playlists" from "different content, similar
       length" outright. This runs on BLU-RAY ONLY: on DVD a title's
       segment map reflects shared VOB files rather than distinct streams,
       so MakeMKV reports matching maps for genuinely different episodes -
       untrustworthy for this purpose - and DVDs don't have the
       duplicate-main-title obfuscation this undoes anyway, so DVDs are
       never deduped (every title kept). It also errs toward keeping on
       Blu-ray: a title whose segment map is missing/empty is always kept.
       On by default; disable with --no-dedupe-duplicate-titles, and
       ignored under --main-playlist.
     - Runaway-output failsafe: the running total size of everything
       extracted from an ISO so far is checked after each title. If it
       grossly exceeds the expected size - the larger of the summed
       per-title MakeMKV estimates and the ISO's own size, plus a margin
       (--runaway-output-margin-pct, default 20%) - extraction of that ISO
       stops immediately, a warning is logged, and the source ISO is left
       in place (not deleted). The margin matters: honest MKV output runs a
       little larger than the raw stream bytes (container overhead plus
       estimate slack), so on a disc whose selected titles nearly fill it
       the total can legitimately tip just over the ISO's own size - the
       failsafe is only meant to catch a GROSS overrun (a looping/duplicate
       extraction, or overlapping playlists that slipped past the
       duplicate-title dedup above), which lands well past the margin.
     - Post-extraction track-count/duration cross-check: after each
       title is extracted, its actual duration and track counts (via
       mkvmerge, or ffprobe if mkvmerge isn't installed) are checked
       against what MakeMKV reported for that title. It's best-effort
       (silently skipped for the whole run if neither tool is found -
       see the warning logged at startup) and warning-level rather than
       a hard stop. The duration comparison is one-sided: only an output
       SHORTER than the reported duration (the truncation direction) is
       flagged; an output slightly longer is a routine measurement
       discrepancy (MakeMKV reports the playlist duration, the probe
       measures the muxed stream) and is ignored. Track counts are NOT
       checked for exact equality, because MakeMKV applies its own
       track-selection rules at extraction time (language preferences,
       etc.), so the output legitimately holds a subset of the disc's
       streams - only an output with MORE tracks than the disc reports,
       or with ZERO audio when the disc had audio, is flagged. Tune the
       duration slack with --duration-tolerance-sec (default 15s);
       disable the whole cross-check with --no-verify-tracks.
     - Non-zero process exit status: the script exits 1 the moment any
       real error is logged (see "Fail-fast" above - a title that failed
       to extract, a disc whose title info couldn't be read, an
       unexpected exception, etc.), 130 if interrupted with Ctrl+C, and 0
       if the whole run completes with no errors - including runs where
       discs were legitimately skipped for benign, expected reasons
       (ambiguous obfuscation, no qualifying titles, etc.), since those
       are normal outcomes, not failures. This makes the run's
       success/failure visible to cron, systemd, or any other wrapper via
       $?.
     - No progress display - not on the console, not in the log - so a run
       is quiet during a title and reports only start/finish per title;
       visibility into a slow title comes from the stall timeout stopping a
       genuinely stuck one, or from watching makemkvcon's disk activity /
       the growing output file externally.
     - Stall timeout: a watchdog aborts a genuinely hung extraction so it
       can't block the batch forever. It watches makemkvcon's PROCESS I/O
       BYTE COUNTERS (the kernel's own per-process read+write accounting,
       via GetProcessIoCounters) - not makemkvcon's progress messages, and
       not the output file's size. This is immune to two separate problems:
       makemkvcon block-buffers its robot-mode output when piped (so those
       messages arrive in unpredictable bursts, and keying on them produced
       false stalls on healthy titles), AND some filesystems report a
       growing file's size lazily (notably a StableBit DrivePool pool). The
       kernel's byte counters tick up in real time as makemkvcon actually
       moves data, regardless of output filesystem. If the process moves no
       bytes AND makemkvcon's percentage doesn't advance for
       --stall-timeout-min minutes (default 15), makemkvcon is terminated
       (reporting how much was written and how long it ran, to tell a real
       stall from a slow disc). Set 0 to disable. It's a stall detector,
       not a wall-clock limit, so a slow-but-progressing large title is
       never killed.
       A stall is the one ERROR that does NOT abort the run: a single hung
       or unreadable disc shouldn't cost the rest of a multi-TB batch, so
       the rest of that ISO's titles are skipped, its source file is left
       in place, and the batch moves on to the next ISO. It's still logged
       at ERROR level, counted in the summary's stalled tally, and makes
       the run exit nonzero, so it can't pass unnoticed.
     - Ctrl+C during a run terminates the in-flight makemkvcon child
       process cleanly, then still prints the summary-so-far and closes
       the log, rather than leaving an orphaned process or a truncated
       log file.
     - Source file date preservation: each output .mkv is stamped with
       the source ISO's access/modification times, so the converted file
       carries the same file date as the disc image it came from (handy
       for chronological sorting and for anything downstream that keys off
       mtime). Best-effort - a filesystem that refuses the timestamp set
       is logged as a warning, not a failure.
     - Every makemkvcon command actually run (both the `info` scan and
       each title's `mkv` extraction) has its verbatim command line
       written to the log file, tagged [CMD] - but never printed to the
       console, which would just be noise for a normal run. This is
       purely for troubleshooting (e.g. spotting a bad path or quoting
       issue after the fact); dry-run's own "[DRY RUN] Would run: ..."
       preview is unrelated and still prints to the console as before,
       since nothing is actually happening in that mode.

8. --include=REGEX / --exclude=REGEX (mutually exclusive - argparse
   rejects passing both) filter the discovered ISO list before any
   processing starts. The regex is matched with re.search (no anchoring
   required) against each ISO's full resolved path, not just the
   filename, so you can filter by a folder name too (e.g. a show or
   season directory) as well as by filename. --include keeps only
   matching files; --exclude keeps only non-matching files. A summary of
   how many files matched is logged once, before the batch begins.

9. Each ISO's output directory mirrors its location relative to --input,
   it does NOT flatten everything directly under --output. E.g. an ISO at
   <input>/Show/S1E1/s1e1.iso produces output under
   <output>/Show/S1E1/s1e1/ - the same relative "Show/S1E1" folder
   structure, plus the usual per-ISO folder named after the ISO's own
   stem. This also governs where resume support (point 7) looks for an
   already-converted ISO's output.

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
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_LOG_NAME: str = "convert.log"  # default log filename; placed in the output folder unless --log overrides

# MakeMKV robot-mode attribute IDs (see module docstring, point 1)
ATTR_TYPE: int = 1  # stream "Type" attribute on SINFO lines - text value "Video"/"Audio"/"Subtitles"
ATTR_NAME: int = 2
ATTR_DURATION: int = 9
ATTR_DISKSIZE_BYTES: int = 11
ATTR_SOURCE_FILENAME: int = 16  # title "Source file name" - the source playlist (e.g. "00610.mpls") on Blu-ray
ATTR_SEGMENTS_MAP: int = 26  # title "Segment map" - the ordered source segments (.m2ts) the playlist references; identical maps == identical content
ATTR_INFO: int = 30  # title "info/comment" text; carries "(FPL_MainFeature)" when JRE identifies it

# Exact marker MakeMKV writes into a title's info text when its BD-Java
# analysis (requires JRE) has identified that title as the main feature.
# Matched with parentheses so we don't also match variants like
# "FPL_MainFeature_UR" (seen on some discs with alternate cuts), which
# need manual disambiguation rather than being silently treated as equal.
FPL_MAIN_FEATURE_RE: re.Pattern[str] = re.compile(r"\(FPL_MainFeature\)")
FPL_SUBSTRING: str = "FPL_MainFeature"

# Exact, confirmed MakeMKV message (see e.g. makemkv.com/bdjava/ and its own
# forums) printed when a disc actually needs BD-Java (fake-playlist
# protection, BD+ handshake, or Soft-KCD) but no JRE could be found. This is
# a much stronger, more specific signal than the mere absence of the
# "Using Java runtime" success line, which also doesn't appear on discs
# that never needed Java in the first place.
JRE_MISSING_MARKER: str = "This disc requires Java runtime (JRE), but none was found"

DVD_MAX_SIZE_GB_DEFAULT: float = 8.5  # decimal GB (10**9 bytes), matching how DVD-9 capacity is marketed
DISC_TYPE_DVD: str = "DVD"
DISC_TYPE_BLURAY: str = "BLURAY"

# Margin for the runaway-output failsafe (see the check in process_iso).
# Extracted MKV output legitimately runs a little larger than the raw stream
# bytes MakeMKV reports - container overhead (headers, cues/seek index) plus
# estimate slack - so on a disc whose selected titles nearly fill it, the
# honest total can tip a fraction of a percent past the ISO's own size. The
# failsafe only wants to catch GROSS overruns (a looping/duplicate
# extraction, typically tens of percent to multiples over), so it allows this
# much headroom above the expected size before tripping.
RUNAWAY_OUTPUT_MARGIN_PCT_DEFAULT: float = 20.0

# Size-based playlist-obfuscation signal (see detect_size_obfuscation). A
# small disc that reports many large titles whose estimated sizes sum to far
# more than the disc can physically hold is presenting decoy playlists over
# the same underlying content - the sizes only sum so high because the decoys
# overlap. This complements the duration-cluster signal (detect_obfuscation),
# which only fires when the decoys share a duration; some discs (e.g. "You're
# Next") deliberately spread decoy durations across minutes to defeat exactly
# that, but can't hide from the physical size math. Two guards, to stay off
# legitimate discs: at least this many candidate titles (so a handful of
# genuinely overlapping cuts can't trip it), AND a combined estimate this
# many times the ISO size (distinct real titles - movie+extras, TV episodes,
# even multi-angle - sum to about the disc size, i.e. a ratio near 1).
OBFUSCATION_SIZE_MIN_TITLES: int = 8
OBFUSCATION_MAX_SIZE_RATIO_DEFAULT: float = 3.0

# Default stall timeout: if a title extraction makes no forward progress
# (makemkvcon's overall percentage doesn't advance) for this many minutes,
# it's treated as hung and aborted. A *stall* rather than a wall-clock limit
# so a legitimately large, slow-but-progressing title isn't killed - only a
# genuinely stuck one. 0 disables it (see --stall-timeout-min).
STALL_TIMEOUT_MIN_DEFAULT: float = 15.0

# How often the stall watchdog wakes to sample makemkvcon's process I/O
# counters. Small relative to the (minutes-long) stall timeout so a stall is
# caught promptly; the sample itself is a cheap in-memory kernel query.
STALL_CHECK_INTERVAL_SEC: float = 15.0

# Sanity floor for "does this look like a real output file", used both to
# verify a title actually got extracted before deleting the source (safety
# enhancement 1) and to decide whether an existing output directory counts
# as "already converted" for resume support (enhancement 4). This is a
# floor, not a quality check - any real title clearing --min-length will
# produce something far larger than this.
MIN_OUTPUT_FILE_BYTES: int = 1_000_000  # 1 MB

# Per-output-folder manifest written on a successful conversion, used by
# resume support (workflow enhancement 4) instead of a plain .mkv file
# *count* comparison - see selection_fingerprint()/manifest_mismatch_reason()
# for why a count alone can't tell "same titles, already done" apart from "a
# different run left a coincidentally-equal number of files here".
MANIFEST_FILENAME: str = ".iso_to_mkv_manifest.json"

# Tolerance (seconds) for comparing a stored source-ISO mtime against a
# freshly stat'd one during the resume check. Exact float equality is too
# brittle: mtime precision is not stable across filesystems (exFAT/FAT round
# to 2s, some network shares and backup/restore round-trips drop sub-second
# precision), so an unchanged ISO can stat a hair different between the run
# that wrote the manifest and a later run that reads it - which, under exact
# equality, would needlessly re-convert a multi-GB disc that was already
# done. A couple of seconds absorbs that jitter while still catching a
# genuine re-authoring of the source (which moves mtime by far more). This
# mirrors the same-purpose tolerance already used when stamping output
# files with the source date.
MTIME_MATCH_TOLERANCE_SEC: float = 2.0


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


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


def csv_fields(payload: str) -> list[str]:
    """Parse a single line of MakeMKV robot-mode output (the part after the
    leading TAG: prefix) into fields.

    This is deliberately NOT run through Python's csv module. MakeMKV's
    robot-mode format looks CSV-like but uses its own escaping convention
    inside quoted fields: an embedded double-quote or backslash is escaped
    with a leading backslash (\\" and \\\\), not by doubling the quote
    ("") the way RFC-4180 (and csv.reader) expects. A disc/title name or
    format string containing a quote or backslash would make csv.reader
    treat the escaped \\" as an early close-quote and mis-split everything
    after it in that line. This parser follows MakeMKV's own convention
    instead, so those fields come through intact."""
    fields: list[str] = []
    i, n = 0, len(payload)
    while True:
        if i < n and payload[i] == '"':
            i += 1  # skip opening quote
            buf: list[str] = []
            while i < n and payload[i] != '"':
                if payload[i] == "\\" and i + 1 < n and payload[i + 1] in ("\\", '"'):
                    buf.append(payload[i + 1])
                    i += 2
                else:
                    buf.append(payload[i])
                    i += 1
            i += 1  # skip closing quote (if the field was well-formed)
            fields.append("".join(buf))
        else:
            # Unquoted field - not normally emitted by MakeMKV for string
            # values, but tolerated here rather than assumed impossible.
            j = payload.find(",", i)
            end = n if j == -1 else j
            fields.append(payload[i:end])
            i = end
        if i < n and payload[i] == ",":
            i += 1
            continue
        break
    return fields


def classify_disc(iso_path: Path, max_dvd_bytes: float, override: str | None) -> str:
    """Heuristically classify an ISO as DVD or Blu-ray. See module
    docstring point 2a-DVD for rationale and known limitations."""
    if override:
        return DISC_TYPE_DVD if override == "dvd" else DISC_TYPE_BLURAY
    size = iso_path.stat().st_size
    return DISC_TYPE_BLURAY if size > max_dvd_bytes else DISC_TYPE_DVD


def preflight_check_makemkvcon() -> str | None:
    """Confirm makemkvcon64.exe (the 64-bit MakeMKV CLI) can be found on
    PATH before processing any files, so a missing install fails fast with
    one clear message instead of every ISO in the batch failing
    individually with the same root cause (safety enhancement 2). The
    64-bit binary is required deliberately: the 32-bit makemkvcon.exe is
    memory-limited and can choke on large Blu-ray/UHD discs, so this script
    will not silently fall back to it."""
    if shutil.which("makemkvcon64.exe") is None:
        return (
            "makemkvcon64.exe (the 64-bit MakeMKV CLI) was not found on PATH. This script "
            "requires the 64-bit binary specifically - it will not use the 32-bit makemkvcon.exe. "
            "Install MakeMKV and add its folder (typically C:\\Program Files (x86)\\MakeMKV) to "
            "your PATH, or copy makemkvcon64.exe somewhere already on PATH."
        )
    return None


def snapshot_output_dir(out_dir: Path) -> dict[str, int]:
    """Map of filename -> size for every file currently in out_dir."""
    if not out_dir.is_dir():
        return {}
    return {p.name: p.stat().st_size for p in out_dir.iterdir() if p.is_file()}


def clear_previous_outputs(out_dir: Path, logger: "DualLogger", stats: "Stats") -> None:
    """Remove a previous run's output artifacts (every .mkv plus the resume
    manifest) from out_dir before re-extracting into it, so each conversion
    starts from a clean slate.

    Why this is needed: makemkvcon always writes its output under its own
    disc-label-derived name (e.g. MovieTitle_t00.mkv), which this script then
    renames to the standardized title_NN.mkv / main_title.mkv. That rename
    has a collision guard that keeps MakeMKV's raw name if the target already
    exists. When an ISO is re-extracted into a folder that still holds the
    previous run's title_NN.mkv (resume didn't skip it - no matching
    manifest, or --force), that guard would fire on the ISO's OWN prior
    output, leaving the movie-named file plus an orphaned stale one - i.e.
    identical input producing title_NN.mkv one run and MovieTitle_t00.mkv the
    next. Clearing first makes the whole operation idempotent.

    Scoped deliberately to this script's own artifacts (*.mkv and the
    manifest), not a blanket wipe, so anything the user added to the folder
    by hand (cover art, external subtitles, notes) is left untouched. The
    manifest is removed too: once its .mkv files are gone it describes state
    that no longer exists, and leaving it could let a later run 'resume-skip'
    against files this one deleted.

    A file we can't delete would let the collision bug recur, so a removal
    failure is treated as a hard error (which, with fail-fast, aborts the
    run) rather than being swallowed."""
    if not out_dir.is_dir():
        return
    stale = [
        p for p in out_dir.iterdir()
        if p.is_file() and (p.suffix.lower() == ".mkv" or p.name == MANIFEST_FILENAME)
    ]
    if not stale:
        return
    # file_only: housekeeping detail worth keeping in the log for
    # troubleshooting, but just noise on-screen during a long batch.
    logger.file_only(
        "INFO",
        f"Re-extracting into a folder with {len(stale)} file(s) from a previous run - "
        f"clearing them first for a clean, idempotent conversion: "
        f"{', '.join(sorted(p.name for p in stale))}",
    )
    for p in stale:
        try:
            p.unlink()
        except OSError as e:
            # logger.error() is fail-fast: this aborts the run. Leaving a
            # stale title_NN.mkv in place would reproduce the exact naming
            # inconsistency this clearing exists to prevent.
            logger.error(f"Could not remove previous output file {p} before re-extracting: {e}")


def selection_fingerprint(args: argparse.Namespace) -> dict[str, Any]:
    """Every argument that can change which titles get picked as candidates
    for a given ISO (independent of the ISO's own content). Used to
    invalidate a previous run's manifest if the command line changes in a
    way that could produce a different candidate set - e.g. a looser
    --min-length or a forced --disc-type could easily select a different
    number (or identity) of titles, and a stale folder from that earlier
    configuration should never be mistaken for a completed run of the
    current one."""
    return {
        "min_length": args.min_length,
        "obfuscation_threshold": args.obfuscation_threshold,
        "obfuscation_tolerance_sec": args.obfuscation_tolerance_sec,
        "main_playlist": args.main_playlist,
        "dvd_max_size_gb": args.dvd_max_size_gb,
        "disc_type": args.disc_type,
        "detect_playall": args.detect_playall,
        "playall_tolerance_sec": args.playall_tolerance_sec,
        "playall_cluster_tolerance_pct": args.playall_cluster_tolerance_pct,
        "dedupe_duplicate_titles": args.dedupe_duplicate_titles,
    }


def read_manifest(out_dir: Path) -> dict[str, Any] | None:
    """Best-effort read of a previous run's manifest from out_dir. Returns
    None if there isn't one, or it can't be parsed (treated the same as
    "no manifest" - resume just won't fire, which is the safe direction to
    fail in)."""
    manifest_path = out_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        data: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    # A manifest that parsed to something other than an object (e.g. a bare
    # list or number) is treated as "no valid manifest".
    return data if isinstance(data, dict) else None


def preserve_source_timestamps(src_stat: os.stat_result, dest_paths: list[Path]) -> list[str]:
    """Copy the source ISO's access/modification times onto each output
    file, so an archived .mkv carries the same file date as the disc image
    it came from (useful for chronological sorting and for downstream tools
    that key off mtime). Full nanosecond precision is used where the
    filesystem supports it. Best-effort per file: returns the names of any
    files whose timestamp couldn't be set (e.g. a read-only filesystem), so
    the caller can warn without failing the conversion."""
    failed: list[str] = []
    for p in dest_paths:
        try:
            os.utime(p, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
        except OSError:
            failed.append(p.name)
    return failed


def stamp_outputs_with_source_date(
    iso_path: Path,
    out_dir: Path,
    filenames: list[str],
    logger: "DualLogger",
    stats: "Stats",
) -> None:
    """Set each named output file's mtime/atime to the source ISO's date,
    for both the fresh-conversion path and the resume-skip path (so a
    re-run repairs outputs that predate this behavior, without needing
    --force). Idempotent and quiet: a file whose mtime already matches the
    source (within a 2s tolerance, so low-resolution filesystems like
    FAT/exFAT don't get re-stamped every run) is left alone. Best-effort -
    a filesystem that refuses the timestamp set is logged as a warning,
    never a hard failure."""
    try:
        src_stat = iso_path.stat()
    except OSError as e:
        logger.warning(f"Could not read source file date to apply to outputs: {e}", iso_path)
        stats.warnings += 1
        return

    to_set: list[Path] = []
    for name in filenames:
        p = out_dir / name
        try:
            if not p.is_file():
                continue
            # A tolerance (MTIME_MATCH_TOLERANCE_SEC) absorbs FAT/exFAT's
            # 2-second mtime granularity so matching files aren't needlessly
            # re-stamped on every run - the same jitter the resume check
            # tolerates when matching the source ISO's mtime.
            if abs(p.stat().st_mtime - src_stat.st_mtime) > MTIME_MATCH_TOLERANCE_SEC:
                to_set.append(p)
        except OSError:
            to_set.append(p)  # can't compare - attempt the set anyway

    if not to_set:
        return

    failed = preserve_source_timestamps(src_stat, to_set)
    applied = len(to_set) - len(failed)
    if applied > 0:
        logger.info(f"Applied source file date to {applied} output file(s)", iso_path)
    if failed:
        logger.warning(
            f"Could not set source file date on {len(failed)} output file(s): {', '.join(failed)}",
            iso_path,
        )
        stats.warnings += 1


def write_manifest(
    out_dir: Path,
    iso_path: Path,
    candidate_tids: list[int],
    output_filenames: list[str],
    args: argparse.Namespace,
    logger: "DualLogger",
    stats: "Stats",
) -> None:
    """Record exactly what this run extracted, and under what selection
    settings, so a future run can tell whether an existing output folder
    really is "this ISO, fully converted with today's settings" rather
    than just "the right number of .mkv files happen to be sitting here".

    A failure to write this is warned about rather than silently ignored:
    the conversion itself succeeded (so aborting the run would be wrong),
    but without the manifest a later run can't resume-skip this ISO and
    will re-convert it - exactly the reliability problem the "--limit then
    re-run" workflow depends on avoiding. Surfacing it lets the user notice
    a persistent cause (e.g. a read-only output tree) instead of silently
    redoing hours of work every run."""
    stat: os.stat_result = iso_path.stat()
    manifest: dict[str, Any] = {
        "iso_size": stat.st_size,
        "iso_mtime": stat.st_mtime,
        "candidate_title_ids": sorted(candidate_tids),
        "output_filenames": sorted(output_filenames),
        "selection_fingerprint": selection_fingerprint(args),
    }
    try:
        (out_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning(
            f"Conversion succeeded but the resume manifest couldn't be written "
            f"({out_dir / MANIFEST_FILENAME}: {e}). This ISO will be re-converted instead of "
            f"skipped on the next run - check that the output folder is writable.",
            iso_path,
        )
        stats.warnings += 1


def manifest_mismatch_reason(
    manifest: dict[str, Any] | None,
    iso_path: Path,
    candidate_tids: list[int],
    out_dir: Path,
    args: argparse.Namespace,
) -> str | None:
    """Decide whether a previous run's manifest lets us safely resume-skip
    this ISO. Returns None when it's a clean match (skip is safe); otherwise
    a short human-readable reason it doesn't match, so the caller can log
    *why* an ISO is being re-converted instead of skipped - which matters a
    lot for the "--limit, then re-run" workflow, where an ISO silently
    failing to skip is the difference between resuming and redoing hours of
    work.

    A clean match means the manifest describes exactly this ISO (by size and
    - within a tolerance - mtime), exactly this candidate title selection,
    exactly this run's selection-relevant settings, AND every file it
    recorded is still present and still looks like real output. This is
    deliberately identity-by-metadata (size + mtime), never a content hash:
    hashing a multi-GB ISO on every run would be ruinously slow and
    disk-bound, defeating the point of a fast resume check."""
    if manifest is None:
        return "no manifest from a previous run"
    try:
        stat = iso_path.stat()
    except OSError as e:
        return f"could not read the source ISO to compare against the manifest ({e})"

    recorded_size = manifest.get("iso_size")
    if recorded_size != stat.st_size:
        return f"source ISO size changed since conversion ({recorded_size} -> {stat.st_size} bytes)"

    # Tolerant mtime comparison - see MTIME_MATCH_TOLERANCE_SEC for why exact
    # float equality was too brittle to rely on for skipping.
    recorded_mtime = manifest.get("iso_mtime")
    if not isinstance(recorded_mtime, (int, float)):
        return "manifest is missing a usable source modification time"
    if abs(float(recorded_mtime) - stat.st_mtime) > MTIME_MATCH_TOLERANCE_SEC:
        return "source ISO modification time changed since conversion (the file looks replaced/re-authored)"

    if manifest.get("candidate_title_ids") != sorted(candidate_tids):
        return (
            "the set of selected titles differs from last time (e.g. --min-length or --disc-type "
            "changed, or the disc now scans to a different title set)"
        )
    if manifest.get("selection_fingerprint") != selection_fingerprint(args):
        return "a title-selection setting changed since the last run"

    for name in manifest.get("output_filenames", []):
        p = out_dir / name
        try:
            if not p.is_file():
                return f"a recorded output file is missing ({name})"
            if p.stat().st_size < MIN_OUTPUT_FILE_BYTES:
                return f"a recorded output file looks truncated ({name})"
        except OSError as e:
            return f"a recorded output file couldn't be verified ({name}: {e})"
    return None


def resolve_probe_tool() -> str | None:
    """Which external tool (if any) is available to verify an extracted
    .mkv file's actual audio/subtitle track counts and duration against
    what MakeMKV reported for the source title (workflow enhancement 7).
    Resolved once per run rather than once per file. mkvmerge is
    preferred (it's the most directly applicable tool for an .mkv file,
    and commonly already installed alongside MakeMKV/MKVToolNix);
    ffprobe is used as a fallback."""
    if shutil.which("mkvmerge"):
        return "mkvmerge"
    if shutil.which("ffprobe"):
        return "ffprobe"
    return None


def probe_output_tracks_and_duration(mkv_path: Path, tool: str) -> tuple[int, int, float] | None:
    """Best-effort probe of an already-extracted .mkv file's audio track
    count, subtitle track count, and duration (seconds), using whichever
    external tool resolve_probe_tool() found. Returns None on any
    failure (tool not found, bad/unparseable output, timeout) - the
    cross-check is then simply skipped for that title rather than
    treated as a failure in its own right, since this is a best-effort
    extra check layered on top of the file-existence/size check that
    already gates source deletion."""
    try:
        if tool == "mkvmerge":
            proc = subprocess.run(
                ["mkvmerge", "-J", str(mkv_path)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=60,
            )
            data = json.loads(proc.stdout)
            tracks = data.get("tracks", [])
            audio = sum(1 for t in tracks if t.get("type") == "audio")
            subs = sum(1 for t in tracks if t.get("type") == "subtitles")
            duration_ns = data.get("container", {}).get("properties", {}).get("duration")
            duration_sec = (duration_ns / 1_000_000_000.0) if duration_ns else 0.0
            return audio, subs, duration_sec

        if tool == "ffprobe":
            proc = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-print_format", "json",
                    "-show_entries", "stream=codec_type:format=duration", str(mkv_path),
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=60,
            )
            data = json.loads(proc.stdout)
            streams = data.get("streams", [])
            audio = sum(1 for s in streams if s.get("codec_type") == "audio")
            subs = sum(1 for s in streams if s.get("codec_type") == "subtitle")
            duration_sec = float(data.get("format", {}).get("duration", 0.0) or 0.0)
            return audio, subs, duration_sec
    except (subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError, OSError):
        pass
    return None


def check_free_space(out_dir: Path, output_root: Path, required_bytes: int, margin_pct: float) -> str | None:
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


def free_bytes_on_volume(path: Path) -> int | None:
    """Free bytes on the volume containing `path`, walking up to the first
    ancestor that actually exists (the output tree may not be created yet
    in a dry run). Returns None if it can't be determined."""
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

class DualLogger:
    """Writes to the console (a short ISO label) and to a log file (full ISO
    path). The console label is the ISO's path relative to the scanned input
    root (e.g. 'Breaking Bad/1-1.ISO', or just '1-1.ISO' for an ISO directly
    in the root), so which show/movie a line refers to is visible at a glance
    without the full path's noise. Pass iso_root (the --input root) to enable
    that; without it, the label is the filename only."""

    def __init__(self, log_path: Path, iso_root: Path | None = None) -> None:
        self.log_path: Path = log_path
        self.iso_root: Path | None = iso_root
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # Append mode: successive runs accumulate in one log rather than
        # overwriting. The "Run started" banner below separates runs.
        self._fh: TextIO = open(self.log_path, "a", encoding="utf-8")
        self._raw(f"==== Run started {datetime.now().isoformat(timespec='seconds')} ====")

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _raw(self, line: str) -> None:
        self._fh.write(line + "\n")
        self._fh.flush()

    def append_raw_to_file(self, line: str) -> None:
        self._raw(line)

    def _console_label(self, iso_path: Path) -> str:
        """The ISO identifier for console lines: its path RELATIVE TO the
        scanned input root, so which show/movie a line refers to is visible
        without the full absolute path's noise. For '<root>/Breaking Bad/
        1-1.ISO' that's 'Breaking Bad/1-1.ISO'; for an ISO sitting directly
        in the root it's just '1-1.ISO' (no prefix); deeper nesting shows the
        whole relative path. Forward slashes regardless of platform, matching
        how --include/--exclude render paths. Falls back to the filename
        alone when no input root is known or the ISO isn't under it. The log
        file always records the full absolute path regardless."""
        if self.iso_root is not None:
            try:
                return iso_path.relative_to(self.iso_root).as_posix()
            except ValueError:
                pass
        return iso_path.name

    def _log(self, level: str, message: str, iso_path: Path | None) -> None:
        ts = self._timestamp()
        console_target = f" {self._console_label(iso_path)}:" if iso_path is not None else ""
        file_target = f" {iso_path}:" if iso_path is not None else ""
        print(f"{ts} [{level}]{console_target} {message}")
        self._raw(f"{ts} [{level}]{file_target} {message}")

    def file_only(self, level: str, message: str, iso_path: Path | None = None) -> None:
        """Same formatting as _log, but never printed to the console - for
        detail (like a verbatim command line) that's worth having in the
        log for troubleshooting but would just be noise on-screen."""
        ts = self._timestamp()
        file_target = f" {iso_path}:" if iso_path is not None else ""
        self._raw(f"{ts} [{level}]{file_target} {message}")

    def info(self, message: str, iso_path: Path | None = None) -> None:
        self._log("INFO", message, iso_path)

    def warning(self, message: str, iso_path: Path | None = None) -> None:
        self._log("WARNING", message, iso_path)

    def error(self, message: str, iso_path: Path | None = None) -> None:
        """Log the error, then fail fast: abort the whole run immediately.

        Every failure path in this script (a title-info scan failure, a
        failed extraction, an unexpected exception, etc.) already funnels
        through this method before deciding what to do next, so putting
        the exit here - rather than at each of those call sites - makes
        "log an error -> stop the run" apply everywhere at once. Nothing
        after the logger.error(...) call that triggered this will run
        (SystemExit isn't caught by the `except Exception` blocks used
        elsewhere in this script, so it propagates straight out).

        Use error_continue() for the deliberate exceptions - failures that
        are known to be isolated to one ISO and shouldn't stop the batch.
        logger.warning() also still lets the batch continue.
        """
        self._log("ERROR", message, iso_path)
        self._raw(f"==== Run aborted after error (fail-fast) {self._timestamp()} ====")
        self.close()
        sys.exit(1)

    def error_continue(self, message: str, iso_path: Path | None = None) -> None:
        """Log at ERROR level WITHOUT aborting the run - for a failure that's
        confined to the current ISO and shouldn't stop the whole batch (the
        stall timeout is the case this exists for: a single hung/unreadable
        disc shouldn't cost the rest of a multi-TB run).

        Identical output to error(); the only difference is that the run
        continues. The caller is responsible for abandoning the current ISO
        (leaving its source file in place) and for counting the error, so the
        run still ends with a nonzero exit status.
        """
        self._log("ERROR", message, iso_path)

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------
# makemkvcon interaction
# --------------------------------------------------------------------------

def format_cmd_for_log(cmd: list[str]) -> str:
    """Render a command list as a line that can be pasted straight into a
    Windows console and run as-is.

    Every path-bearing argument is wrapped in double quotes, so paths
    containing spaces (common here: "Halloween III Season of the Witch
    (1982).iso", "The Shield (2002)") survive the copy/paste instead of being
    split into several arguments by the shell. An argument is treated as
    path-bearing when it contains a path separator, a drive-letter prefix, or
    a space - which covers both the plain output folder and the "iso:<path>"
    form. The WHOLE argument is quoted, including that iso: prefix
    ("iso:C:\\...\\x.iso"), because cmd.exe strips the quotes and hands
    makemkvcon the intact single argument it expects.

    Switches and other non-path arguments (-r, --cache=1, mkv, the title
    number) are left bare so the line still reads naturally. Windows paths
    can't contain a double quote, so no escaping is needed inside them.

    This is for logging only - the actual subprocess call passes the argument
    list directly and never goes through a shell, so quoting here can't affect
    how the command really runs."""
    parts: list[str] = []
    for arg in cmd:
        looks_like_path = (
            "\\" in arg
            or "/" in arg
            or " " in arg
            or re.match(r"^(?:[a-zA-Z]+:)?[a-zA-Z]:", arg) is not None  # C:\... or iso:C:\...
        )
        parts.append(f'"{arg}"' if looks_like_path and not arg.startswith('"') else arg)
    return " ".join(parts)


def run_cmd(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return proc.returncode, proc.stdout or ""
    except FileNotFoundError as e:
        return 127, f"Could not execute command {cmd!r}: {e}"


def _terminate_proc(proc: "subprocess.Popen[str]") -> None:
    """Terminate a child process, escalating to kill if it doesn't stop."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class _IO_COUNTERS(ctypes.Structure):
    """Mirror of the Win32 IO_COUNTERS struct filled by GetProcessIoCounters.
    All six fields are cumulative-since-process-start ULONGLONGs."""
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


# Lazily-bound kernel32.GetProcessIoCounters: None = not yet tried, False =
# tried and unavailable, else the callable. Bound on first use (rather than at
# import) so the module still imports anywhere for tooling/tests, without a
# platform check in the hot path.
_get_process_io_counters: "object | None" = None


def _process_io_bytes(proc: "subprocess.Popen[str]") -> int:
    """Total bytes this process has transferred so far (read + write + other),
    via Windows GetProcessIoCounters - the kernel's own per-process I/O
    accounting. This is the stall watchdog's ground-truth "is makemkvcon
    actually doing work" signal: it updates in real time as makemkvcon reads
    the ISO and writes the MKV, independently of (a) makemkvcon's stdout
    buffering and (b) whatever filesystem the output lands on - so it behaves
    identically on a StableBit DrivePool pool and on plain NTFS, sidestepping
    any pool-layer file-size caching. Returns -1 if the counters can't be read.

    Note: counts only the makemkvcon process we launched, not any child
    processes it might spawn; makemkvcon64.exe does its own I/O, so this holds
    in practice."""
    global _get_process_io_counters
    if _get_process_io_counters is None:
        try:
            fn = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessIoCounters
            fn.restype = ctypes.c_int  # BOOL
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_IO_COUNTERS)]
            _get_process_io_counters = fn
        except Exception:
            _get_process_io_counters = False
    if not _get_process_io_counters:
        return -1
    try:
        handle = int(proc._handle)  # Popen's process handle (Windows)
    except Exception:
        return -1
    counters = _IO_COUNTERS()
    if not _get_process_io_counters(ctypes.c_void_p(handle), ctypes.byref(counters)):
        return -1
    return int(
        counters.ReadTransferCount + counters.WriteTransferCount + counters.OtherTransferCount
    )


def run_cmd_with_progress(
    cmd: list[str],
    stall_timeout_sec: float | None = None,
) -> tuple[int, str, bool]:
    """Like run_cmd, but streams output while a long title extraction runs and
    drives a stall watchdog, so a genuinely hung makemkvcon is aborted instead
    of blocking the batch forever. Returns (returncode, output, stalled);
    stalled is True when the watchdog killed the child (returncode will be
    nonzero from the kill).

    IMPORTANT - what "progress" means here. The stall clock is reset by the
    makemkvcon PROCESS'S I/O BYTE COUNTERS advancing (the kernel's own
    per-process read+write accounting, via GetProcessIoCounters), NOT by
    makemkvcon's progress *messages* and NOT by the output file's size. This is
    deliberate and defends against two independent problems at once:
      - makemkvcon block-buffers its stdout when it's a pipe, so its robot-mode
        PRGV progress lines arrive in irregular bursts (or barely at all)
        during a long extraction even while it's working perfectly - keying on
        them produced false stalls on healthy titles (random across runs, as
        flush timing is nondeterministic); and
      - the output file's *size* can be reported lazily by some filesystems
        (notably a StableBit DrivePool pool, which updates real-time size
        tracking oriented around file close), which could look like a stall.
    The kernel's I/O byte counters have neither problem: they tick up as
    makemkvcon actually moves bytes to/from disk, in real time, regardless of
    output filesystem. Received PRGV advances still count as a secondary "still
    alive" reset (harmless - it can only prevent a false kill, never cause
    one). A stall - and a kill - happens only when NEITHER the process moved
    any bytes NOR progress advanced for stall_timeout_sec.

    We still drain stdout (so makemkvcon never blocks on a full pipe) and keep
    the full captured output for post-hoc error diagnostics, same as run_cmd.

    On Ctrl+C, explicitly terminates (then kills, if needed) the child
    process before re-raising, so an interrupt doesn't leave an orphaned
    makemkvcon running in the background (workflow enhancement 6)."""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
    except FileNotFoundError as e:
        return 127, f"Could not execute command {cmd!r}: {e}", False

    lines: list[str] = []
    last_activity = [time.monotonic()]  # reset on process I/O advance OR progress advance
    highest_pct = [-1.0]
    stalled = threading.Event()
    stop_watchdog = threading.Event()

    watchdog: threading.Thread | None = None
    if stall_timeout_sec is not None and stall_timeout_sec > 0:
        def _watch() -> None:
            last_io = _process_io_bytes(proc)
            while not stop_watchdog.wait(STALL_CHECK_INTERVAL_SEC):
                io = _process_io_bytes(proc)
                if io >= 0 and io > last_io:  # process moved real bytes since last check
                    last_io = io
                    last_activity[0] = time.monotonic()
                if time.monotonic() - last_activity[0] > stall_timeout_sec:
                    stalled.set()
                    _terminate_proc(proc)
                    return
        watchdog = threading.Thread(target=_watch, daemon=True)
        watchdog.start()

    try:
        assert proc.stdout is not None
        # readline() rather than "for line in proc.stdout" so lines are handed
        # over as they arrive instead of waiting for Python's iterator read-
        # ahead buffer to fill - one less layer of buffering between us and
        # makemkvcon.
        for raw_line in iter(proc.stdout.readline, ""):
            line = raw_line.rstrip("\n")
            lines.append(line)
            # A received progress advance is a secondary "still alive" signal
            # (see docstring); the process I/O counters in the watchdog are the
            # primary one. Only a real advance counts, so a fixed-percent
            # process that keeps re-emitting the same PRGV doesn't mask a stall.
            if line.startswith("PRGV:"):
                fields = csv_fields(line.removeprefix("PRGV:"))
                if len(fields) >= 3:
                    try:
                        _current, total, maxv = int(fields[0]), int(fields[1]), int(fields[2])
                    except ValueError:
                        continue
                    if maxv > 0:
                        pct = min(100.0, max(0.0, (total / maxv) * 100.0))
                        if pct > highest_pct[0]:
                            highest_pct[0] = pct
                            last_activity[0] = time.monotonic()
    except KeyboardInterrupt:
        stop_watchdog.set()
        _terminate_proc(proc)
        if watchdog is not None:
            watchdog.join(timeout=5)
        raise
    finally:
        stop_watchdog.set()
        if proc.poll() is None:
            proc.wait()
        if watchdog is not None:
            watchdog.join(timeout=5)

    return proc.returncode, "\n".join(lines), stalled.is_set()


@dataclass
class Title:
    title_id: int
    duration_sec: float = 0.0
    size_bytes: int = 0
    name: str | None = None
    info_text: str | None = None  # attribute 30; may contain "(FPL_MainFeature)"
    source_filename: str | None = None  # attribute 16; the source playlist e.g. "00610.mpls" on Blu-ray
    segments_map: str | None = None  # attribute 26; ordered source segments the playlist references (e.g. "1,2,3") - the content-identity signal used for duplicate detection
    audio_track_count: int = 0       # from SINFO lines - used by the post-extraction cross-check
    subtitle_track_count: int = 0    # from SINFO lines - used by the post-extraction cross-check


def get_disc_titles(
    iso_path: Path, logger: "DualLogger"
) -> tuple[int, str, dict[int, Title], bool, bool]:
    """Run `makemkvcon info` on the ISO and parse the title table.

    Returns (returncode, raw_output, titles, jre_engaged, jre_required_missing).
    jre_engaged is True if makemkvcon reported it launched the Java runtime
    for this disc (i.e. it attempted BD-Java based main-feature detection).
    jre_required_missing is True if this disc specifically needed Java (for
    fake-playlist protection, a BD+ handshake, or Soft-KCD) and MakeMKV
    could not find a JRE to use - a much stronger signal than jre_engaged
    simply being False, which is also true for every disc that never
    needed Java at all."""
    cmd: list[str] = ["makemkvcon64.exe", "-r", "--cache=1", "info", f"iso:{iso_path}"]
    logger.file_only("CMD", format_cmd_for_log(cmd), iso_path)
    rc, output = run_cmd(cmd)
    titles: dict[int, Title] = {}
    jre_engaged: bool = "Using Java runtime" in output
    jre_required_missing: bool = JRE_MISSING_MARKER in output

    for line in output.splitlines():
        line = line.strip()
        if line.startswith("TINFO:"):
            fields = csv_fields(line.removeprefix("TINFO:"))
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
            elif code == ATTR_SOURCE_FILENAME:
                t.source_filename = value
            elif code == ATTR_SEGMENTS_MAP:
                t.segments_map = value
            elif code == ATTR_INFO:
                t.info_text = value
        elif line.startswith("SINFO:"):
            # SINFO:title_id,stream_id,attribute_id,code,"value" - same
            # attribute-id/code/value shape as TINFO, with a stream_id
            # inserted after title_id. Only the Type attribute is used
            # here, to count audio/subtitle tracks per title for the
            # post-extraction cross-check (workflow enhancement 7).
            fields = csv_fields(line.removeprefix("SINFO:"))
            if len(fields) < 5:
                continue
            try:
                tid, attr_id = int(fields[0]), int(fields[2])
            except ValueError:
                continue
            if attr_id != ATTR_TYPE:
                continue
            value = fields[4]
            t = titles.setdefault(tid, Title(title_id=tid))
            if value == "Audio":
                t.audio_track_count += 1
            elif value == "Subtitles":
                t.subtitle_track_count += 1

    return rc, output, titles, jre_engaged, jre_required_missing


def looks_like_warning(output: str) -> str | None:
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

def normalize_playlist_name(name: str) -> str:
    """Normalize a Blu-ray source-playlist identifier for comparison, so a
    user-supplied --main-playlist matches MakeMKV's reported "Source file
    name" regardless of superficial differences. Lowercases, strips
    surrounding whitespace and any directory part, and drops a trailing
    ".mpls" so "00610.mpls", "00610", "00610.MPLS", and "PLAYLIST/00610.mpls"
    all compare equal."""
    n = name.strip().lower().replace("\\", "/")
    n = n.rsplit("/", 1)[-1]  # keep only the basename
    return n.removesuffix(".mpls")


def find_title_by_playlist(titles: dict[int, Title], requested: str) -> list[int]:
    """Return the title IDs whose source playlist filename matches
    `requested` (normalized). Normally exactly one; more than one would
    mean two titles share a source playlist (unusual), which the caller
    treats as ambiguous. Titles with no reported source filename can't
    match and are skipped."""
    want = normalize_playlist_name(requested)
    return [
        tid for tid, t in titles.items()
        if t.source_filename and normalize_playlist_name(t.source_filename) == want
    ]


def detect_obfuscation(
    titles: dict[int, Title], threshold: int, tolerance_sec: float
) -> tuple[bool, float, int]:
    """Find the largest cluster of titles whose durations all fall within
    a single tolerance_sec-wide window; if that cluster is at least
    `threshold` titles, suspect playlist obfuscation. Returns
    (suspected, representative_duration, cluster_size).

    A sliding tolerance window is used rather than exact round-to-second
    bucketing because obfuscation decoys are frequently only *similar* in
    length, not frame-identical (per MakeMKV/AVS forum reports of discs
    with "same or similar" length playlists). Exact-second bucketing
    would split one logical cluster across adjacent buckets and undercount
    it - e.g. durations of 89.4s and 89.6s round to 89 and 90 and would be
    counted as two clusters of one instead of one cluster of two. Sorting
    the durations and sweeping a window of width tolerance_sec over them
    groups "close enough" decoys together, so the threshold is compared
    against the true cluster size. tolerance_sec = 0 reproduces the old
    exact-match behavior (modulo float equality)."""
    if not titles:
        return False, 0.0, 0
    durations = sorted(t.duration_sec for t in titles.values())
    best_count = 0
    best_center = 0.0
    left = 0
    # Classic "largest set of sorted points spanning <= W" two-pointer
    # sweep: for each right edge, advance left until the window span fits
    # within tolerance_sec, then record the widest count seen.
    for right in range(len(durations)):
        while durations[right] - durations[left] > tolerance_sec:
            left += 1
        count = right - left + 1
        if count > best_count:
            best_count = count
            best_center = (durations[left] + durations[right]) / 2.0
    return best_count >= threshold, best_center, best_count


def detect_size_obfuscation(
    candidates: list[int],
    titles: dict[int, Title],
    iso_size_bytes: int,
    min_titles: int,
    max_size_ratio: float,
) -> tuple[bool, int, float]:
    """Detect playlist obfuscation from the physical size math rather than
    from duration clustering. Returns (suspected, estimated_total_bytes,
    ratio).

    Rationale: every candidate playlist reports an estimated size (the sum of
    the segments it references). Distinct, real titles reference distinct
    segments, so their estimates sum to roughly what's physically on the disc
    - a combined-estimate-to-ISO ratio near 1 (a movie plus extras, a set of
    TV episodes, even multiple camera angles, all sit around there). Decoy
    playlists instead reference the SAME underlying segments over and over, so
    each one still reports near-main-feature size and the estimates sum to a
    large multiple of what the disc can actually hold. A ratio far above 1 is
    therefore a reliable obfuscation signature - and unlike duration
    clustering, it doesn't care whether the decoys share a runtime, so it
    catches discs that spread decoy durations out to dodge that check.

    Guarded twice to stay off legitimate discs: it needs at least min_titles
    candidates (so a couple of genuinely overlapping alternate cuts can't trip
    it) AND a ratio above max_size_ratio. Returns suspected=False (never a
    false alarm) when the ISO size is unknown."""
    estimated_total = sum(titles[t].size_bytes for t in candidates)
    if iso_size_bytes <= 0:
        return False, estimated_total, 0.0
    ratio = estimated_total / iso_size_bytes
    suspected = len(candidates) >= min_titles and ratio > max_size_ratio
    return suspected, estimated_total, ratio


MIN_CANDIDATES_FOR_PLAYALL_DETECTION: int = 3  # need the concat title plus >= 2 episodes


def detect_playall_title(
    candidates: list[int],
    titles: dict[int, Title],
    tolerance_sec: float,
    cluster_tolerance_pct: float,
) -> tuple[int, list[int]] | None:
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


def normalize_segments_map(raw: str | None) -> str | None:
    """Canonicalize a title's segment map (MakeMKV attribute 26) for equality
    comparison. The map is an ordered, comma-separated list of the source
    segments a playlist references (e.g. "1,2,3"); whitespace around entries
    is normalized away but ORDER IS PRESERVED, since two playlists that
    reference the same segments in a different order are not the same content.
    Returns None for a missing/empty map so callers can treat "no segment
    information" distinctly from "a real map"."""
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return None
    return ",".join(parts)


def dedupe_duplicate_titles(
    candidates: list[int],
    titles: dict[int, "Title"],
) -> tuple[list[int], list[tuple[int, int]]]:
    """Collapse duplicate main titles - the same feature exposed as several
    titles - down to one copy each, using each title's SEGMENT MAP (the
    ordered set of source .m2ts segments its playlist references) as the test
    for "same content".

    INTENDED FOR BLU-RAY ONLY; the caller must gate this on disc type. The
    segment map (MakeMKV attribute 26) is a trustworthy content fingerprint on
    Blu-ray, where each title is a playlist over its own distinct .m2ts stream
    segments. It is NOT trustworthy on DVD: a DVD's titles (PGCs) within one
    VTS all reference the same shared VOB files, so MakeMKV reports identical
    or overlapping segment maps for genuinely different episodes - which would
    make this function merge distinct episodes (as seen on "V (2009)" S2D2).
    This function itself can't tell the two formats apart from the title data,
    so it trusts the segment map at face value; keeping it Blu-ray-only is the
    caller's responsibility.

    Some Blu-rays present the main feature as multiple playlists that all
    point at the same underlying segments (seamless-branching artifacts,
    redundant playlists, or a mild anti-ripping tactic). When no single
    (FPL_MainFeature) marker singles one out, the fallback selection keeps
    every title over --min-length, so all the copies get extracted: identical
    output written more than once, and a combined size that can exceed the
    source ISO and trip the runaway-output failsafe. Keeping one copy is
    almost always what was wanted.

    Crucially, duplicates are detected by segment-map identity, NOT by
    similar duration and size. Duration+size similarity is the wrong signal
    for this: distinct TV episodes on the same disc routinely share a runtime
    to the second and a size to a fraction of a percent (same show, same
    target length, same encode settings), so any duration/size tolerance
    loose enough to catch real duplicates also merges genuinely different
    episodes. On Blu-ray the segment map is a content fingerprint instead -
    two different episodes occupy different .m2ts segments, while true
    duplicate playlists occupy exactly the same ones.

    Erring toward caution: a title is only ever dropped when its (non-empty)
    segment map is byte-identical to one already kept. Any title whose
    segment map is missing/empty is always kept - "can't prove it's a
    duplicate" resolves to "keep it", never "drop it". The lowest title_id of
    each duplicate group is kept, so output naming stays deterministic run to
    run. Returns (kept_tids_sorted, dropped) where dropped is a list of
    (dropped_tid, kept_representative_tid) for logging."""
    kept: list[int] = []
    dropped: list[tuple[int, int]] = []
    seen: dict[str, int] = {}  # normalized segment map -> representative tid already kept
    for tid in sorted(candidates):
        segmap = normalize_segments_map(titles[tid].segments_map)
        if segmap is None:
            # No segment information -> cannot establish it's a duplicate ->
            # keep it. This is the cautious default that protects TV discs
            # and anything that doesn't report a segment map.
            kept.append(tid)
            continue
        representative = seen.get(segmap)
        if representative is None:
            seen[segmap] = tid
            kept.append(tid)
        else:
            dropped.append((tid, representative))
    return kept, dropped


def unique_output_dir(output_root: Path, relative_dir: Path, stem: str, used: set[str]) -> Path:
    """Mirrors the ISO's directory structure relative to --input under
    output_root, so e.g. <input>/Show/S1E1/s1e1.iso produces
    <output>/Show/S1E1/s1e1/ rather than flattening everything directly
    under output_root. used is keyed on the full relative output path
    (not just the stem), since preserving structure already makes
    same-stem collisions across different subfolders a non-issue - this
    disambiguation now only matters for two ISOs landing on the exact
    same relative output path, which realistically shouldn't happen
    given each input ISO has a distinct path, but the safety net costs
    nothing to keep."""
    name = stem
    n = 2
    key = str(relative_dir / name)
    while key in used:
        name = f"{stem}_{n}"
        key = str(relative_dir / name)
        n += 1
    used.add(key)
    return output_root / relative_dir / name


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
    isos_stalled: int = 0  # abandoned mid-extraction by the stall timeout (non-fatal; batch continued)


@dataclass
class ProcessResult:
    """Outcome of process_iso, richer than a plain bool so main() can
    drive the resume/limit logic around it - and, in dry-run, assemble a
    pre-flight plan (space forecast, needs-attention list, and summary)
    without extracting anything.

    info_scan_failed and unexpected_error are set but no longer acted on
    in main(): any error that would set one of these fields is logged via
    DualLogger.error() first, which already aborts the run before
    process_iso() gets a chance to return. They're kept on the dataclass
    (harmlessly unreachable) rather than ripped out, in case a future
    change makes some category of failure non-fatal again. stalled IS
    reachable: a stall timeout is deliberately non-fatal, so process_iso
    returns normally and main() counts the ISO as failed-but-skipped."""
    converted: bool = False           # fully converted this run (counts toward --limit, eligible for deletion)
    skipped_already_done: bool = False  # resume: output already existed, nothing was done
    info_scan_failed: bool = False    # couldn't even read title info
    unexpected_error: bool = False    # an unhandled exception was caught around this ISO
    stalled: bool = False             # abandoned because a title extraction stalled (non-fatal; batch continues)
    # --- dry-run planning fields (populated on every path, consumed only by the dry-run report) ---
    titles_selected: int = 0          # how many titles would be / were extracted
    estimated_output_bytes: int = 0   # sum of selected titles' MakeMKV-reported sizes (output-size estimate)
    would_delete_source: bool = False  # dry-run: source would be deleted (if --delete-source and output verifies)
    needs_attention: bool = False     # a skip the user should resolve before a real run (vs a benign resume skip)
    attention_reason: str | None = None  # short label for the needs-attention report
    jre_missing: bool = False         # this disc needed a JRE that MakeMKV couldn't find (a fixable root cause)


# --------------------------------------------------------------------------
# Per-ISO processing
# --------------------------------------------------------------------------


def obfuscation_stop(
    logger: "DualLogger",
    stats: "Stats",
    args: argparse.Namespace,
    iso_path: Path,
    reason: str,
    jre_missing: bool,
) -> "ProcessResult":
    """Handle a disc that looks obfuscated and can't be safely converted -
    dozens of decoy playlists with no resolvable main feature.

    In a real run this is a fail-fast error: it logs the reason (with guidance
    on how to proceed for this disc) and exits, so the run doesn't grind
    through extracting a runaway pile of fake titles. In dry-run it instead
    logs a warning and returns a needs-attention result, so the survey keeps
    going and lists every problem disc rather than aborting at the first one.
    The returned ProcessResult is only reached in dry-run; in a real run
    logger.error() exits before the return."""
    guidance = (
        " - not converting this ISO. If you can research the correct main-feature playlist for "
        "this disc, re-run it with --main-playlist <name>; otherwise exclude it from the batch."
    )
    if args.dry_run:
        logger.warning(f"{reason}{guidance} (dry-run: skipping, not aborting)", iso_path)
        stats.warnings += 1
    else:
        logger.error(f"{reason}{guidance}", iso_path)  # fail-fast: exits the run here
    return ProcessResult(
        needs_attention=True,
        attention_reason=reason,
        jre_missing=jre_missing,
    )

def process_iso(
    iso_path: Path,
    input_root: Path,
    output_root: Path,
    args: argparse.Namespace,
    logger: DualLogger,
    stats: Stats,
    used_output_names: set[str],
    probe_tool: str | None,
    bytes_converted_so_far: int = 0,
) -> ProcessResult:
    """Returns a ProcessResult describing what happened, so main() can
    drive --limit accounting, source deletion, and the info-scan
    circuit breaker.

    bytes_converted_so_far is the run-wide total of source ISO bytes
    converted before this ISO (the same figure --limit tracks and the
    end-of-run summary reports); it's used only to annotate the per-title
    extraction log with cumulative batch progress."""

    min_length_sec = args.min_length * 60.0

    # The ISO's directory structure relative to --input is mirrored under
    # --output (e.g. <input>/Show/S1E1/s1e1.iso -> <output>/Show/S1E1/s1e1/)
    # rather than flattening every ISO's output directly under output_root.
    try:
        relative_dir = iso_path.parent.relative_to(input_root)
    except ValueError:
        # Shouldn't happen given iso_path came from rglob(input_root), but
        # fall back to flat output rather than crashing if it somehow does.
        logger.warning(
            f"Could not determine {iso_path}'s folder relative to --input {input_root} - "
            f"placing its output directly under the output root instead",
            iso_path,
        )
        stats.warnings += 1
        relative_dir = Path(".")

    # --- Resume support (workflow enhancement 4) ---
    # Checked against the disc's natural (non-disambiguated) output path,
    # since that's what a previous run would have used. Only a whole-ISO
    # check is done - if a multi-title disc was interrupted partway
    # through, this deliberately does NOT try to resume just the missing
    # titles (predicting makemkvcon's own output filenames well enough to
    # do that safely is fragile); it will just fully redo that one ISO,
    # which is the safe default over a false "looks done" skip.
    natural_out_dir = output_root / relative_dir / iso_path.stem
    previous_manifest = None if args.force else read_manifest(natural_out_dir)

    disc_type_override = None if args.disc_type == "auto" else args.disc_type
    disc_type = classify_disc(iso_path, args.dvd_max_size_gb * 1_000_000_000, disc_type_override)
    # file_only: useful when diagnosing a disc-type misclassification after the
    # fact, but not worth a console line for every ISO in a long batch.
    logger.file_only(
        "INFO", f"Classified as {disc_type} ({human_bytes(iso_path.stat().st_size)})", iso_path
    )

    rc, output, titles, jre_engaged, jre_required_missing = get_disc_titles(iso_path, logger)
    if rc != 0 or not titles:
        logger.error(f"Failed to read title information (exit code {rc})", iso_path)
        logger.append_raw_to_file(output)
        stats.conversions_error += 1
        return ProcessResult(
            info_scan_failed=True,
            needs_attention=True,
            attention_reason="title-info scan failed (unreadable disc, or makemkvcon registration/permissions)",
        )

    # Set below only when MakeMKV's own (FPL_MainFeature) marker identifies
    # a title - not when this script's own duration-based fallback picks
    # one. That title's output gets named "main_title.mkv" (see the
    # per-title loop) so other tools can trust the filename rather than
    # re-deriving which title was the main feature.
    fpl_identified_main_tid: int | None = None

    if args.main_playlist:
        # --- Manual main-title override (one-off for unresolvable obfuscation) ---
        # The user has researched the correct source playlist (e.g. via a
        # community post) for a disc MakeMKV couldn't resolve, and passed it
        # as --main-playlist. This deliberately bypasses all automatic
        # detection (FPL marker, duration clustering, the "ambiguous ->
        # skip" guard) - the human has resolved the ambiguity. We match on
        # the source PLAYLIST filename, not a MakeMKV title index, because
        # title indices aren't stable across versions or --min-length
        # settings, whereas the playlist name is exactly what gets looked
        # up. main() guarantees the run resolved to a single ISO before we
        # get here, so the disc-specific playlist name can't be misapplied
        # across discs.
        matches = find_title_by_playlist(titles, args.main_playlist)
        if not matches:
            available = ", ".join(
                f"{t.source_filename or '?'} ({format_duration(t.duration_sec)})"
                for _, t in sorted(titles.items())
            )
            logger.error(
                f"--main-playlist '{args.main_playlist}' did not match any title's source "
                f"playlist on this disc. Available titles: {available}",
                iso_path,
            )
            stats.conversions_error += 1
            return ProcessResult(
                needs_attention=True,
                attention_reason="--main-playlist matched no title on this disc",
            )
        if len(matches) > 1:
            logger.error(
                f"--main-playlist '{args.main_playlist}' matched {len(matches)} titles "
                f"{sorted(matches)} - ambiguous, not proceeding",
                iso_path,
            )
            stats.conversions_error += 1
            return ProcessResult(
                needs_attention=True,
                attention_reason="--main-playlist matched multiple titles (ambiguous)",
            )

        main_tid = matches[0]
        main_duration = titles[main_tid].duration_sec
        # Keep genuine extras (deleted scenes, featurettes) but exclude the
        # decoy cluster: decoys sit at the main feature's own duration, so
        # anything NOT clearly shorter than the chosen title (by more than
        # the duration-equality tolerance) is treated as a same-length
        # decoy and dropped. Longer titles are dropped too - the user asked
        # for shorter extras only.
        extras = sorted(
            tid for tid, t in titles.items()
            if tid != main_tid
            and t.duration_sec >= min_length_sec
            and (main_duration - t.duration_sec) > args.obfuscation_tolerance_sec
        )
        candidates = [main_tid] + extras
        fpl_identified_main_tid = main_tid  # human-verified pick -> named main_title.mkv
        logger.info(
            f"Manual override: title {main_tid} (playlist "
            f"{titles[main_tid].source_filename}, {format_duration(main_duration)}) selected as "
            f"the main title; also keeping {len(extras)} shorter title(s) {extras} as extras "
            f"(same-length decoys excluded)",
            iso_path,
        )

    elif disc_type == DISC_TYPE_DVD:
        # Blu-ray-specific detection (BD-Java / FPL_MainFeature, and the
        # duration-clustering fallback that exists to catch Blu-ray-style
        # ScreenPass decoys) doesn't apply to DVDs - see docstring point
        # 2a-DVD. Just take every title that passes --min-length.
        candidates = [tid for tid, t in titles.items() if t.duration_sec >= min_length_sec]

    else:
        if jre_required_missing:
            logger.warning(
                "This disc requires a Java runtime (JRE) for BD-Java processing (fake-playlist "
                "protection, a BD+ handshake, or Soft-KCD), but MakeMKV could not find one - "
                "install a JRE or set app_Java in MakeMKV's settings.conf. See "
                "https://www.makemkv.com/bdjava/",
                iso_path,
            )
            stats.warnings += 1

        suspected, dup_duration, dup_count = detect_obfuscation(
            titles, args.obfuscation_threshold, args.obfuscation_tolerance_sec
        )

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
                return ProcessResult(
                    needs_attention=True,
                    attention_reason="multiple (FPL_MainFeature) markers (ambiguous)",
                    jre_missing=jre_required_missing,
                )
            main_tid = fpl_exact[0]
            if titles[main_tid].duration_sec < min_length_sec:
                logger.warning(
                    f"Title {main_tid} was flagged (FPL_MainFeature) but is shorter than "
                    f"--min-length ({format_duration(titles[main_tid].duration_sec)}) - skipping disc",
                    iso_path,
                )
                stats.warnings += 1
                return ProcessResult(
                    needs_attention=True,
                    attention_reason="(FPL_MainFeature) title is shorter than --min-length",
                )
            logger.info(
                f"MakeMKV's Java-based analysis identified title {main_tid} as (FPL_MainFeature)",
                iso_path,
            )
            candidates = [main_tid]
            fpl_identified_main_tid = main_tid

        else:
            # --- Signal 2 (fallback): duration-clustering heuristic ---
            # No "did not identify a main title" line is logged here on
            # purpose: on many discs MakeMKV never emits an (FPL_MainFeature)
            # marker, so logging its absence every time is pure noise. The
            # positive case is logged above ("...identified title N as
            # (FPL_MainFeature)"); anything genuinely actionable on this
            # fallback path (obfuscation, ambiguity) still logs its own
            # warning below.
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
                    # Ambiguous obfuscation: many same-length candidates and no
                    # way to tell which is the real feature. Fail-fast in a real
                    # run (or note it and continue in dry-run).
                    return obfuscation_stop(
                        logger, stats, args, iso_path,
                        reason=(
                            f"Suspected playlist obfuscation: {len(candidates)} candidate titles "
                            f"share ~{format_duration(dup_duration)} and no (FPL_MainFeature) marker "
                            f"resolves the main feature"
                        ),
                        jre_missing=jre_required_missing,
                    )
                # Exactly one unambiguous candidate remains - safe to proceed.

    if not candidates:
        logger.warning(f"No titles >= {args.min_length:g} minutes found - skipping disc", iso_path)
        stats.warnings += 1
        return ProcessResult(
            needs_attention=True,
            attention_reason=f"no titles >= {args.min_length:g} min (nothing would be produced)",
            jre_missing=jre_required_missing,
        )

    # Play-all detection is skipped under a manual override: the candidate
    # set is a hand-picked main title plus deliberately-kept shorter
    # extras, and the play-all heuristic (which tests the longest candidate
    # as a concatenation of the shorter ones) could otherwise wrongly
    # discard the very title the user selected.
    if args.detect_playall and not args.main_playlist:
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
        return ProcessResult(
            needs_attention=True,
            attention_reason="only a 'Play All' title found (no individual episodes to keep)",
        )

    # Collapse duplicate main titles (the same feature under multiple
    # playlists that reference the identical source segments) down to one
    # copy - see dedupe_duplicate_titles(). Skipped under --main-playlist for
    # the same reason play-all detection is: that path is a hand-picked
    # selection and shouldn't be second-guessed. Without this, a disc exposing
    # 2+ copies of the main feature extracts all of them, wasting space on
    # identical output and producing a combined size that can exceed the
    # source ISO and trip the runaway-output failsafe below. Detection is by
    # segment-map identity, not duration/size similarity, so distinct TV
    # episodes that happen to share a runtime and size are never merged.
    #
    # BLU-RAY ONLY. The segment map (MakeMKV attribute 26) is a reliable
    # content fingerprint on Blu-ray, where each title's playlist references
    # its own distinct .m2ts stream segments. On DVD it is NOT: a DVD's titles
    # (PGCs) within one VTS all reference the same shared VOB files, so
    # MakeMKV reports identical/overlapping segment maps for genuinely
    # DIFFERENT episodes (observed on "V (2009)" S2D2, where 5 distinct
    # episodes collapsed to 2). DVDs also don't have the duplicate-main-title
    # obfuscation this exists to undo, so there's nothing to gain and real
    # episodes to lose - we simply don't dedup DVDs, erring toward keeping
    # every title. (The play-all concatenation title is still removed
    # separately, and the runaway/size-obfuscation guards still apply.)
    if (
        args.dedupe_duplicate_titles
        and not args.main_playlist
        and disc_type == DISC_TYPE_BLURAY
        and len(candidates) > 1
    ):
        deduped, dropped_dupes = dedupe_duplicate_titles(candidates, titles)
        for dropped_tid, rep_tid in dropped_dupes:
            logger.info(
                f"Title {dropped_tid} (playlist {titles[dropped_tid].source_filename}, "
                f"{format_duration(titles[dropped_tid].duration_sec)}, "
                f"{human_bytes(titles[dropped_tid].size_bytes)}) references the same source "
                f"segments as title {rep_tid} (playlist {titles[rep_tid].source_filename}) - "
                f"treating it as a duplicate copy of the same content and keeping only title "
                f"{rep_tid}",
                iso_path,
            )
        if dropped_dupes:
            logger.info(
                f"Dropped {len(dropped_dupes)} duplicate title(s); extracting "
                f"{len(deduped)} title(s) instead of {len(candidates)}",
                iso_path,
            )
        candidates = deduped

    # --- Size-based obfuscation guard ---
    # A last check before committing to extraction: if the titles we're about
    # to extract have a combined estimated size far larger than the ISO can
    # physically hold, they're decoy playlists over the same content, not that
    # many distinct titles (see detect_size_obfuscation). This catches the
    # obfuscated discs the duration-cluster signal misses - ones that spread
    # decoy runtimes out by minutes so no tight duration cluster forms - and
    # it does so BEFORE extracting anything (the runaway-output failsafe would
    # otherwise let this run for a long time first, since the inflated
    # per-title estimates also inflate its threshold). Skipped under
    # --main-playlist, where the user has hand-resolved the disc.
    if not args.main_playlist:
        size_obfuscated, est_total, ratio = detect_size_obfuscation(
            candidates, titles, iso_path.stat().st_size,
            OBFUSCATION_SIZE_MIN_TITLES, args.obfuscation_max_size_ratio,
        )
        if size_obfuscated:
            return obfuscation_stop(
                logger, stats, args, iso_path,
                reason=(
                    f"Suspected playlist obfuscation: {len(candidates)} candidate titles have a "
                    f"combined estimated size of {human_bytes(est_total)}, {ratio:.1f}x the "
                    f"{human_bytes(iso_path.stat().st_size)} source ISO - impossible for that many "
                    f"distinct titles, so these are decoy/fake playlists over the same content"
                ),
                jre_missing=jre_required_missing,
            )

    # Resume check happens here, once we know exactly which titles we'd
    # extract - compared against a previous run's manifest (see
    # manifest_mismatch_reason() for why this is more reliable than the
    # old "count the .mkv files" approach: it checks the ISO itself, the
    # exact set of title IDs, and every selection-relevant argument, not
    # just a number that could match by coincidence).
    resume_mismatch = manifest_mismatch_reason(
        previous_manifest, iso_path, candidates, natural_out_dir, args
    )
    if resume_mismatch is None:
        logger.info(
            f"{natural_out_dir} already has a matching completed conversion for this exact "
            f"title selection and these settings - skipping. Use --force to redo.",
            iso_path,
        )
        # Even when skipping, make sure the existing outputs carry the
        # source file date - this repairs folders converted before file-
        # date preservation existed, without needing --force. No-op when
        # they already match (see stamp_outputs_with_source_date).
        if not args.dry_run and previous_manifest:
            stamp_outputs_with_source_date(
                iso_path, natural_out_dir, previous_manifest.get("output_filenames", []), logger, stats
            )
        stats.already_converted_skipped += 1
        return ProcessResult(skipped_already_done=True)
    elif previous_manifest is not None:
        # A manifest existed but didn't clear the resume check. Log the
        # specific reason (not a vague list of possibilities), so an
        # unexpected re-conversion during a "--limit then re-run" workflow
        # is diagnosable at a glance rather than a mystery.
        logger.warning(
            f"{natural_out_dir} has a previous-run manifest but it doesn't match, so this ISO "
            f"will be redone rather than skipped. Reason: {resume_mismatch}",
            iso_path,
        )
        stats.warnings += 1

    out_dir = unique_output_dir(output_root, relative_dir, iso_path.stem, used_output_names)
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        # Reaching here means we've committed to (re)extracting this ISO
        # rather than resume-skipping it. If a previous run left output in
        # this folder, remove it first so extraction starts clean - this is
        # what makes identical input produce identical output names (see
        # clear_previous_outputs() for the full rationale).
        clear_previous_outputs(out_dir, logger, stats)

    all_ok = True
    stop_reason: str | None = None  # set when all_ok is False for a reason other than a title outright failing
    stalled_out: bool = False  # a stall timeout abandoned this ISO - non-fatal, the batch continues
    output_filenames: list[str] = []  # populated on success, written into the manifest below
    # Tracks whether EVERY extracted title was affirmatively verified clean
    # by the track/duration cross-check. Starts True only if a probe tool is
    # available at all; any title that can't be probed, or that shows a
    # mismatch, flips it False. --delete-source consults this so a source is
    # only ever removed when its output was positively confirmed good.
    output_verified = probe_tool is not None
    iso_size_bytes = iso_path.stat().st_size
    total_extracted_bytes = 0  # running total across titles - see size failsafe below
    # Expected output size for the runaway-output failsafe below: the sum of
    # MakeMKV's per-title byte estimates for exactly the titles we're about to
    # extract. This is a more meaningful expectation than the whole ISO's size
    # - it scales to the selected title set and stays correct even when titles
    # legitimately share/overlap disc content (each still contributes its own
    # estimate). The failsafe uses whichever of {this, the ISO size} is
    # larger as its base, so a missing/zero estimate simply falls back to the
    # ISO-size bound and can never make the check stricter.
    estimated_total_bytes = sum(titles[t].size_bytes for t in candidates)
    runaway_output_limit = max(iso_size_bytes, estimated_total_bytes) * (
        1 + args.runaway_output_margin_pct / 100.0
    )
    # Convert the stall timeout to seconds once; None disables the watchdog.
    stall_timeout_sec = (
        args.stall_timeout_min * 60.0
        if args.stall_timeout_min and args.stall_timeout_min > 0
        else None
    )
    for tid in sorted(candidates):
        title = titles[tid]
        logger.info(
            f"Extracting title {tid} (duration {format_duration(title.duration_sec)}) -> {out_dir} "
            f"[{human_bytes(bytes_converted_so_far)} converted so far]",
            iso_path,
        )

        # Every audio/subtitle track on the title is extracted as-is - see
        # docstring point 3 for why this script doesn't attempt track
        # filtering (makemkvcon has no CLI mechanism for it at all, and a
        # prior mkvmerge-based workaround was deliberately removed in
        # favor of leaving track curation to a later encoding pass).
        cmd = ["makemkvcon64.exe", "-r", "--cache=1", "mkv", f"iso:{iso_path}", str(tid), str(out_dir)]

        if args.dry_run:
            logger.info(f"[DRY RUN] Would run: {format_cmd_for_log(cmd)}", iso_path)
            desired_name = "main_title.mkv" if tid == fpl_identified_main_tid else f"title_{tid:02d}.mkv"
            logger.info(f"[DRY RUN] Would name output {desired_name}", iso_path)
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
        before_bytes = sum(before_snapshot.values())
        extract_start = time.monotonic()
        logger.file_only("CMD", format_cmd_for_log(cmd), iso_path)
        rc, mkv_output, stalled = run_cmd_with_progress(cmd, stall_timeout_sec=stall_timeout_sec)

        if stalled:
            # makemkvcon moved no bytes (and its progress didn't advance) for the
            # stall-timeout window, so it was terminated as hung. Unlike most
            # errors here this does NOT abort the run: a single hung or
            # unreadable disc shouldn't cost the rest of a multi-TB batch. We
            # log it at ERROR level (so it stands out and the run still exits
            # nonzero), abandon THIS ISO - stop extracting its remaining titles
            # and leave its source file in place - and let the batch move on to
            # the next ISO. The elapsed time and bytes-written figures make a
            # genuine stall ("wrote 12 GB then stopped") easy to tell from a
            # spurious one at a glance.
            elapsed = time.monotonic() - extract_start
            written = max(0, sum(snapshot_output_dir(out_dir).values()) - before_bytes)
            logger.append_raw_to_file(mkv_output)
            logger.error_continue(
                f"Title {tid} extraction stalled - makemkvcon moved no data for "
                f"{args.stall_timeout_min:g} minute(s) (wrote {human_bytes(written)} in "
                f"{format_duration(elapsed)} before stalling), so it was terminated. The disc may "
                f"have bad sectors or makemkvcon may be hung; investigate this title. Raise or "
                f"disable the limit with --stall-timeout-min if it was just genuinely slow. "
                f"Skipping the rest of this ISO and continuing with the next one.",
                iso_path,
            )
            stats.conversions_error += 1
            stats.isos_stalled += 1
            all_ok = False
            stalled_out = True
            stop_reason = (
                f"title {tid} extraction stalled (no data for {args.stall_timeout_min:g} minute(s))"
            )
            break

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

        # Standardize the output filename so downstream tooling can rely
        # on it instead of parsing MakeMKV's disc-label-derived default.
        # The MakeMKV-identified (or manually overridden) main feature
        # becomes main_title.mkv; every other extracted title becomes
        # title_NN.mkv, where NN is its MakeMKV title number - stable,
        # sortable, and important for TV discs where each title is an
        # episode and the number is how you tell episodes apart. The output
        # folder is cleared of any previous run's files before extraction
        # (see clear_previous_outputs()), so a same-name collision from a
        # prior run can't happen here anymore; this guard is now just a
        # backstop against an unexpected in-run collision or a foreign file,
        # in which case we keep MakeMKV's original filename and warn rather
        # than clobbering anything.
        desired_name = "main_title.mkv" if tid == fpl_identified_main_tid else f"title_{tid:02d}.mkv"
        final_name = qualifying_new_mkvs[0]  # overwritten below only if the rename actually succeeds
        src_path = out_dir / qualifying_new_mkvs[0]
        dest_path = out_dir / desired_name
        if src_path != dest_path:
            if dest_path.exists():
                logger.warning(
                    f"Title {tid}: wanted to name output '{desired_name}' but that file "
                    f"already exists in {out_dir} - leaving it as {qualifying_new_mkvs[0]}",
                    iso_path,
                )
                stats.warnings += 1
            else:
                try:
                    src_path.replace(dest_path)
                    logger.info(f"Title {tid}: named output {desired_name}", iso_path)
                    final_name = desired_name
                except OSError as e:
                    logger.warning(f"Title {tid}: failed to name output {desired_name}: {e}", iso_path)
                    stats.warnings += 1

        output_filenames.append(final_name)
        stats.conversions_success += 1

        # --- Post-extraction track-count/duration cross-check (workflow enhancement 7) ---
        # The size/existence check above only confirms *something* real-
        # sized landed on disk. This adds a duration check (the reliable
        # truncation signal) plus a couple of conservative track-count
        # sanity checks against what MakeMKV reported for the source title.
        # Best-effort only (skipped entirely if neither mkvmerge nor ffprobe
        # is installed) and warning-level rather than a hard stop.
        #
        # Track counts are deliberately NOT checked for exact equality:
        # MakeMKV applies its own track-selection rules at extraction time
        # (language preferences, default selection string, etc.), so the
        # output legitimately contains a SUBSET of the streams the info scan
        # lists - foreign-language audio and subtitle tracks are commonly
        # dropped. Flagging "fewer tracks than the disc" therefore produced
        # false positives on ordinary discs. We only flag what track
        # selection cannot explain: MORE tracks in the output than the disc
        # reports (impossible for MakeMKV to produce - likely the wrong file
        # probed or a miscount), and an output with ZERO audio when the disc
        # had audio (a genuinely broken extraction). A zero-subtitle output
        # is NOT flagged, since stripping all subtitles is a legitimate
        # setting.
        if probe_tool is not None:
            probe = probe_output_tracks_and_duration(out_dir / final_name, probe_tool)
            if probe is not None:
                out_audio, out_subs, out_duration = probe
                mismatches = []
                if out_audio > title.audio_track_count:
                    mismatches.append(
                        f"audio tracks: output has {out_audio}, more than the "
                        f"{title.audio_track_count} the disc reports"
                    )
                elif title.audio_track_count > 0 and out_audio == 0:
                    mismatches.append(
                        f"audio tracks: output has none, but the disc reports "
                        f"{title.audio_track_count}"
                    )
                if out_subs > title.subtitle_track_count:
                    mismatches.append(
                        f"subtitle tracks: output has {out_subs}, more than the "
                        f"{title.subtitle_track_count} the disc reports"
                    )
                # Duration is checked ASYMMETRICALLY: only an output that
                # is meaningfully SHORTER than MakeMKV's reported duration
                # is a truncation signal. An output slightly LONGER is a
                # routine, benign discrepancy - MakeMKV reports the
                # playlist's declared duration, while the probe measures
                # the muxed stream, and the two commonly differ by several
                # seconds (trailing frames, last-segment rounding), with
                # the file usually the longer of the two. Flagging the
                # longer direction produced false positives on normal discs
                # (e.g. TV episode playlists routinely ~10s longer) without
                # ever indicating missing content.
                shortfall = title.duration_sec - out_duration
                if shortfall > args.duration_tolerance_sec:
                    mismatches.append(
                        f"duration: expected {format_duration(title.duration_sec)}, "
                        f"found {format_duration(out_duration)} "
                        f"({shortfall:.0f}s shorter than expected)"
                    )
                if mismatches:
                    logger.warning(
                        f"Title {tid}: post-extraction cross-check flagged an issue against what "
                        f"MakeMKV reported for this title ({'; '.join(mismatches)}) - the output "
                        f"may be truncated or have an unexpected track layout; worth a manual look",
                        iso_path,
                    )
                    stats.warnings += 1
                    output_verified = False  # a flagged title means the disc isn't cleanly verified
            else:
                # Probe tool present but couldn't read this file - can't
                # affirmatively verify it, so the disc isn't clean-verified.
                output_verified = False

        # --- Runaway-output failsafe ---
        # The combined size of everything extracted from this ISO shouldn't
        # grossly exceed what these titles are expected to produce. "Expected"
        # is max(sum of MakeMKV's per-title estimates, the ISO's own size),
        # plus a margin (--runaway-output-margin-pct) for the container
        # overhead and estimate slack that make honest MKV output run a little
        # larger than the raw stream bytes - without that headroom, a disc
        # whose titles nearly fill it can tip a fraction of a percent over and
        # trip this for no reason (e.g. Halloween III: 37.42 GB of legitimate
        # output vs a 37.30 GB ISO). Duplicate main titles are already
        # collapsed during selection (see dedupe_duplicate_titles()), so what
        # remains for this backstop is a GROSS overrun - a genuinely
        # looping/duplicate extraction, or overlapping playlists that slipped
        # past dedup - which lands well past the margin. On a trip, stop
        # pulling more titles from this ISO and leave the source alone rather
        # than risk deleting it (if --delete-source is set) after a broken
        # extraction.
        total_extracted_bytes += after_snapshot[qualifying_new_mkvs[0]]
        if total_extracted_bytes > runaway_output_limit:
            expected_base = max(iso_size_bytes, estimated_total_bytes)
            stop_reason = (
                f"extracted output so far ({human_bytes(total_extracted_bytes)}) exceeds the "
                f"expected size ({human_bytes(expected_base)}) by more than the "
                f"{args.runaway_output_margin_pct:g}% runaway margin "
                f"(limit {human_bytes(runaway_output_limit)})"
            )
            logger.warning(
                f"{stop_reason} - this points to something wrong with the extraction rather "
                f"than genuinely larger output. Stopping further titles for this ISO; source "
                f"file will be retained.",
                iso_path,
            )
            stats.warnings += 1
            all_ok = False
            break

    if not all_ok:
        # A stall was already reported non-fatally above and deliberately does
        # not abort the batch, so this summary line must not either - use the
        # matching non-fatal logger. Every other failure keeps the fail-fast
        # logger.error().
        report = logger.error_continue if stalled_out else logger.error
        if stop_reason:
            report(f"Stopped converting this ISO: {stop_reason}; source file retained", iso_path)
        else:
            report("One or more titles failed to convert; source file retained", iso_path)
        return ProcessResult(stalled=stalled_out)

    logger.info("All selected titles converted successfully", iso_path)
    stats.isos_converted += 1

    if not args.dry_run:
        # Recorded so a future run can tell "already done, same settings"
        # apart from "coincidentally the same file count" - see
        # manifest_mismatch_reason().
        write_manifest(out_dir, iso_path, candidates, output_filenames, args, logger, stats)

        # Stamp each output .mkv with the source ISO's file date. Done
        # before any source deletion (the source still exists here) and
        # only over the final, named output files - not the manifest.
        stamp_outputs_with_source_date(iso_path, out_dir, output_filenames, logger, stats)

    if not args.delete_source:
        logger.info("Keeping source file (deletion is off by default; enable with --delete-source)", iso_path)
    elif args.dry_run:
        logger.info(
            "[DRY RUN] Would delete source ISO file if its output verified clean (--delete-source)",
            iso_path,
        )
    elif not output_verified:
        # --delete-source only removes a source whose output was positively
        # confirmed good by the track/duration cross-check. Here it wasn't
        # (a mismatch, a probe that couldn't read a file, or no probe tool
        # available), so the source is kept regardless of --delete-source.
        # This is the safety gate working as intended, so it's INFO, not an
        # error; any actual mismatch already logged its own warning above.
        logger.info(
            "Not deleting source ISO: its output was not verified clean by the track/duration "
            "cross-check, so it's kept for safety despite --delete-source. Resolve the issue and "
            "re-run, or delete manually once satisfied.",
            iso_path,
        )
    else:
        try:
            iso_path.unlink()
            logger.info("Deleted source ISO file (--delete-source, output verified clean)", iso_path)
        except OSError as e:
            logger.error(f"Failed to delete source file: {e}", iso_path)

    return ProcessResult(
        converted=True,
        titles_selected=len(candidates),
        estimated_output_bytes=sum(titles[t].size_bytes for t in candidates),
        would_delete_source=args.delete_source,
    )


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

def compile_regex_arg(value: str) -> re.Pattern[str]:
    # Case-insensitive per the filtering contract (requirement 2). Anchoring
    # to the start of the input-relative path is done at match time with
    # .match() (requirement 1), not here.
    try:
        return re.compile(value, re.IGNORECASE)
    except re.error as e:
        raise argparse.ArgumentTypeError(f"Invalid regular expression {value!r}: {e}")


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Same as ArgumentDefaultsHelpFormatter, except --no-detect-playall
    shows "(default: enabled)" instead of the literal "(default: True)" -
    which reads as if the disabling flag were itself on by default, when
    it's actually detect_playall (the setting it controls) that defaults
    to True/enabled."""
    def _get_help_string(self, action: argparse.Action) -> str | None:
        if action.dest == "detect_playall":
            return (action.help or "") + " (default: enabled)"
        return super()._get_help_string(action)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Batch-convert .iso files to .mkv using makemkvcon.",
        formatter_class=_HelpFormatter,
    )
    # Defaults are a sentinel (None) rather than "." so we can tell whether
    # the user actually supplied the flag: at least one of -i/-o must be
    # given (see the validation at the end of parse_args), and whichever is
    # omitted then defaults to ".".
    p.add_argument("-i", "--input", default=None,
                   help="Input folder to search recursively for .iso files (default: '.', but "
                        "at least one of -i/-o must be given, and it must be a separate tree "
                        "from -o)")
    p.add_argument("-o", "--output", default=None,
                   help="Output root folder (default: '.', but at least one of -i/-o must be "
                        "given, and it must be a separate tree from -i)")
    p.add_argument(
        "-m", "--min-length", type=float, default=10.0, metavar="MINUTES",
        help="Minimum title length (in minutes) to extract",
    )
    p.add_argument(
        "-l", "--log", nargs="?", const=None, default=None, metavar="LOGFILE",
        help="Log file path. Defaults to 'convert.log' inside the output folder (-o). Pass an "
             "explicit path to place it elsewhere",
    )
    p.add_argument(
        "--delete-source", action="store_true",
        help="Delete each source ISO after it converts AND its output passes the track/duration "
             "cross-check clean. Off by default - sources are kept unless this is given. A source "
             "whose output can't be positively verified (a cross-check mismatch, an unreadable "
             "output, or no mkvmerge/ffprobe available) is always kept, so this has no effect "
             "without a probe tool installed",
    )
    p.add_argument("-n", "--dry-run", action="store_true", help="Show what would happen without changing anything")
    p.add_argument(
        "-L", "--limit", type=float, default=-1, metavar="GB",
        help="Stop once this many GB of ISO source data have been converted (-1 = no limit)",
    )
    filter_group = p.add_mutually_exclusive_group()
    filter_group.add_argument(
        "-I", "--include", type=compile_regex_arg, default=None, metavar="REGEX",
        help="Only process ISOs whose path, relative to the input folder (-i), matches this "
             "regex. Anchored at the start (a partial match from the first character counts, "
             "a match further down the path does not) and case-insensitive. E.g. with -i d:/tmp, "
             "'ab' matches tmp/abcdef.iso but not tmp/xy/ab.iso. Mutually exclusive with --exclude",
    )
    filter_group.add_argument(
        "-X", "--exclude", type=compile_regex_arg, default=None, metavar="REGEX",
        help="Skip any ISO whose path, relative to the input folder (-i), matches this regex "
             "(same anchored-at-start, case-insensitive matching as --include); all others are "
             "processed. Mutually exclusive with --include",
    )
    p.add_argument(
        "--obfuscation-threshold", type=int, default=30, metavar="N",
        help="Number of titles sharing (almost) the same duration that triggers Playlist "
             "Obfuscation suspicion. Legitimate discs rarely have more than a handful of "
             "same-length titles, while obfuscated (ScreenPass/Lionsgate) discs present dozens "
             "to hundreds, so this sits well above normal clustering but low enough to catch "
             "dozens-scale obfuscation",
    )
    p.add_argument(
        "--obfuscation-tolerance-sec", type=float, default=2.0, metavar="SECONDS",
        help="Width of the duration window within which titles are treated as sharing a duration "
             "for obfuscation detection. Decoy playlists are often only *similar* in length, not "
             "frame-identical; this groups them instead of splitting near-equal durations across "
             "buckets. 0 requires (near-)exact duration matches",
    )
    p.add_argument(
        "--obfuscation-max-size-ratio", type=float, default=OBFUSCATION_MAX_SIZE_RATIO_DEFAULT, metavar="X",
        help=f"Second, size-based obfuscation signal (catches decoys whose durations are spread out "
             f"to dodge the duration-cluster check). If the selected titles' combined estimated size "
             f"exceeds the source ISO's size by more than this factor - impossible for that many "
             f"distinct titles, so a sign of decoy playlists over the same content - the disc is "
             f"flagged as obfuscated and not converted (errors out in a real run, noted in dry-run). "
             f"Only applies once there are at least {OBFUSCATION_SIZE_MIN_TITLES} candidate titles, "
             f"so a few genuinely overlapping alternate cuts won't trip it. Default "
             f"{OBFUSCATION_MAX_SIZE_RATIO_DEFAULT:g}",
    )
    p.add_argument(
        "--main-playlist", default=None, metavar="MPLS",
        help="Manually force the main title by its source playlist filename (e.g. '00610.mpls' "
             "or just '00610'), for a one-off conversion of a disc whose obfuscation MakeMKV "
             "couldn't resolve and whose correct playlist you've researched. Bypasses all "
             "automatic main-title detection for that disc. The chosen title is named "
             "main_title.mkv, and every title clearly SHORTER than it (genuine extras) is also "
             "extracted, while same-length decoys are excluded. Because a playlist name is "
             "disc-specific, the run must resolve to exactly one ISO - point --input at a "
             "folder with a single ISO, or narrow a larger one with --include/--exclude; "
             "otherwise the script aborts before processing anything",
    )
    p.add_argument(
        "--dvd-max-size-gb", type=float, default=DVD_MAX_SIZE_GB_DEFAULT, metavar="GB",
        help="ISOs at or below this size (decimal GB) are classified as DVD; larger ones as Blu-ray",
    )
    p.add_argument(
        "-t", "--disc-type", choices=["auto", "dvd", "bluray"], default="auto",
        help="Force disc-type classification for this run instead of using the size heuristic",
    )
    p.add_argument(
        "--no-detect-playall", dest="detect_playall", action="store_false", default=True,
        help="Disable detection of a 'Play All' concatenation title (common on TV-show DVDs); "
             "on by default",
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
        "--no-dedupe-duplicate-titles", dest="dedupe_duplicate_titles", action="store_false", default=True,
        help="Disable collapsing duplicate main titles. By default, when a disc exposes the same "
             "feature as multiple playlists that reference the identical source segments (common on "
             "Blu-rays with redundant/seamless-branching playlists), only one copy is extracted "
             "instead of all of them. Duplicates are identified by segment-map identity, not by "
             "similar duration/size, so distinct TV episodes that share a runtime and size are "
             "never merged; titles without a reported segment map are always kept. Disable this to "
             "extract every qualifying title regardless. Ignored under --main-playlist. On by default",
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
        "--stall-timeout-min", type=float, default=STALL_TIMEOUT_MIN_DEFAULT, metavar="MINUTES",
        help="Abort (fail-fast) if a title extraction makes no forward progress for this many "
             "minutes - makemkvcon's overall percentage not advancing, i.e. a hung extraction or a "
             "disc it can't read past. This is a STALL timeout, not a wall-clock limit: a slow but "
             "steadily-progressing large title is left alone, only a genuinely stuck one is killed. "
             f"Default {STALL_TIMEOUT_MIN_DEFAULT:g}; set 0 to disable",
    )
    p.add_argument(
        "--runaway-output-margin-pct", type=float, default=RUNAWAY_OUTPUT_MARGIN_PCT_DEFAULT, metavar="PCT",
        help="How far the combined extracted output may exceed its expected size - the larger of "
             "the summed per-title estimates and the ISO's own size - before the runaway-output "
             "failsafe stops the ISO. The headroom absorbs MKV container overhead and estimate "
             "slack (honest output can run a little over the raw stream bytes); the failsafe is "
             "only meant to catch gross overruns like a looping/duplicate extraction. Raise it if "
             "a legitimate disc trips it, lower it to catch smaller overruns",
    )
    p.add_argument(
        "--no-verify-tracks", dest="verify_tracks", action="store_false", default=True,
        help="Disable the post-extraction audio/subtitle track-count and duration cross-check "
             "against what MakeMKV reported for the title (requires mkvmerge or ffprobe to be "
             "installed; silently skipped if neither is found). On by default",
    )
    p.add_argument(
        "--duration-tolerance-sec", type=float, default=15.0, metavar="SECONDS",
        help="How many seconds SHORTER than MakeMKV's reported duration the extracted file may "
             "be before the cross-check flags it as possibly truncated. The check is one-sided: "
             "an output longer than reported is a normal measurement discrepancy and is never "
             "flagged; only a shortfall beyond this many seconds is",
    )
    args = p.parse_args()

    # At least one of -i/-o must be supplied - otherwise both would default
    # to "." and the input and output trees would be identical, which the
    # separation rule below forbids anyway.
    if args.input is None and args.output is None:
        p.error("at least one of -i/--input or -o/--output must be provided")
    if args.input is None:
        args.input = "."
    if args.output is None:
        args.output = "."

    # The input and output folders must be separate trees: not the same
    # folder, and neither nested inside the other. Nesting output under
    # input would drop conversions into the source tree; nesting input
    # under output invites a later run treating produced files as sources
    # and generally muddles which tree is which. Compare resolved absolute
    # paths so "." vs an equivalent absolute path, symlinks, and ".." are
    # all normalized first.
    in_res = Path(args.input).resolve()
    out_res = Path(args.output).resolve()
    if in_res == out_res:
        p.error(f"input and output folders must be different (both resolve to {in_res})")
    if out_res.is_relative_to(in_res):
        p.error(f"output folder ({out_res}) must not be inside the input folder ({in_res}) - "
                f"use separate trees")
    if in_res.is_relative_to(out_res):
        p.error(f"input folder ({in_res}) must not be inside the output folder ({out_res}) - "
                f"use separate trees")

    return args


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    input_root = Path(args.input).resolve()
    output_root = Path(args.output).resolve()
    # Default the log into the output folder; an explicit --log path (if
    # given) is honored as-is, relative to the current directory.
    log_path = (output_root / DEFAULT_LOG_NAME) if args.log is None else Path(args.log).resolve()

    logger = DualLogger(log_path, iso_root=input_root)

    if args.dry_run:
        logger.info("Running in DRY RUN mode - no files will be changed")

    # --- Pre-flight check (safety enhancement 2) ---
    # Fail fast on a bad/missing makemkvcon rather than letting every ISO
    # in the batch fail individually with the same root cause.
    preflight_error = preflight_check_makemkvcon()
    if preflight_error:
        logger.error(preflight_error)
        logger.error("Aborting before processing any files - ensure makemkvcon64.exe is in your PATH")
        logger.close()
        return 1

    # --- Post-extraction verification tool (workflow enhancement 7) ---
    # Resolved once for the whole run rather than per-file. If neither
    # tool is installed, the cross-check is simply skipped for every
    # title - logged once here so it's clear why, rather than silently
    # never firing.
    probe_tool = resolve_probe_tool() if args.verify_tracks else None
    if args.verify_tracks and probe_tool is None:
        logger.warning(
            "Neither mkvmerge nor ffprobe was found - the post-extraction track-count/duration "
            "cross-check will be skipped for this entire run. Install MKVToolNix or ffmpeg to "
            "enable it, or pass --no-verify-tracks to silence this message."
        )

    # --delete-source now removes a source only after its output passes the
    # cross-check clean. If that check can't run this session, deletion can
    # never be authorized, so warn up front rather than silently keeping
    # every source.
    if args.delete_source and probe_tool is None:
        reason = "--no-verify-tracks is set" if not args.verify_tracks else "no mkvmerge/ffprobe found"
        logger.warning(
            f"--delete-source only deletes a source after its output is verified clean by the "
            f"track/duration cross-check, but that check is unavailable this run ({reason}). "
            f"No sources will be deleted. Install mkvmerge or ffprobe (and don't pass "
            f"--no-verify-tracks) to enable verified deletion."
        )

    if not input_root.is_dir():
        logger.error(f"Input folder does not exist: {input_root}")
        logger.close()
        return 1

    if not args.dry_run:
        output_root.mkdir(parents=True, exist_ok=True)

    iso_files = sorted(
        {p for p in input_root.rglob("*") if p.is_file() and p.suffix.lower() == ".iso"}
    )

    # --include/--exclude match against each ISO's path RELATIVE to the
    # input folder, as a forward-slash string (so patterns are the same on
    # Windows and Unix), and are anchored at the start via .match() - a
    # partial match from the first character counts, but a match further
    # down the path does not. So with -i d:/tmp, --include=ab matches
    # d:/tmp/abcdef.iso (relative "abcdef.iso") but not d:/tmp/xy/ab.iso
    # (relative "xy/ab.iso"). Matching is case-insensitive (see
    # compile_regex_arg).
    if args.include:
        before = len(iso_files)
        iso_files = [p for p in iso_files if args.include.match(p.relative_to(input_root).as_posix())]
        logger.info(
            f"--include={args.include.pattern!r} applied: {len(iso_files)} of {before} "
            f"ISO(s) matched and will be processed"
        )
    elif args.exclude:
        before = len(iso_files)
        iso_files = [p for p in iso_files if not args.exclude.match(p.relative_to(input_root).as_posix())]
        logger.info(
            f"--exclude={args.exclude.pattern!r} applied: {before - len(iso_files)} of {before} "
            f"ISO(s) matched and will be skipped"
        )

    if not iso_files:
        logger.info(f"No .iso files found under {input_root}")
        logger.close()
        return 0

    # A --main-playlist name is disc-specific (playlist 00610.mpls means
    # something different, or nothing, on another disc), so applying it
    # across a multi-ISO run would be wrong. Require the run to resolve to
    # exactly one ISO - the user narrows with --include/--exclude - and
    # abort loudly otherwise rather than silently forcing the wrong title
    # on the wrong disc.
    if args.main_playlist and len(iso_files) != 1:
        logger.error(
            f"--main-playlist is a one-off, single-disc override but this run matched "
            f"{len(iso_files)} ISO files. Narrow it to exactly one - point --input at a "
            f"folder with a single ISO, or filter with --include/--exclude - and re-run. "
            f"Not processing anything."
        )
        logger.close()
        return 1

    total_bytes_all = sum(p.stat().st_size for p in iso_files)
    total_count = len(iso_files)
    limit_bytes = args.limit * (1024 ** 3) if args.limit and args.limit > 0 else None

    logger.info(
        f"Found {total_count} ISO file(s), {human_bytes(total_bytes_all)} total, "
        f"under {input_root}"
    )

    stats: Stats = Stats()
    used_output_names: set[str] = set()

    start_time: float = time.time()
    bytes_time_processed: int = 0
    bytes_converted_running: int = 0
    interrupted: bool = False

    # --- Dry-run plan accumulators (consumed only when args.dry_run) ---
    dry_convert_count: int = 0
    dry_titles_total: int = 0
    dry_output_bytes: int = 0
    dry_convert_input_bytes: int = 0
    dry_resume_skips: int = 0
    dry_attention: list[tuple[Path, str, bool]] = []  # (iso_path, reason, jre_missing)
    dry_delete_count: int = 0
    dry_delete_bytes: int = 0

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

            # A crash while processing one ISO (a malformed robot-mode
            # line, an unexpected filesystem error, etc.) is caught here so
            # it's logged against this specific ISO rather than surfacing
            # as a raw traceback - but logger.error() below still aborts
            # the whole run immediately (fail-fast). Only KeyboardInterrupt
            # is allowed to propagate past this without going through the
            # logger first.
            try:
                result = process_iso(
                    iso_path, input_root, output_root, args, logger, stats, used_output_names, probe_tool,
                    bytes_converted_so_far=bytes_converted_running,
                )
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(
                    f"Unexpected error while processing this ISO: {e!r}",
                    iso_path,
                )
                stats.conversions_error += 1
                result = ProcessResult(unexpected_error=True)

            # Note: there's no circuit breaker here anymore. Since
            # DualLogger.error() now aborts the run immediately, any
            # failure inside process_iso() (or the "unexpected error"
            # except block above) already stops everything before
            # execution gets back to this point.

            bytes_time_processed += iso_size
            if result.converted:
                bytes_converted_running += iso_size
                stats.bytes_converted += iso_size

            # Gather the dry-run pre-flight plan as we go (see the plan
            # printed after the loop). Cheap, and only reported in dry-run.
            if args.dry_run:
                if result.converted:
                    dry_convert_count += 1
                    dry_titles_total += result.titles_selected
                    dry_output_bytes += result.estimated_output_bytes
                    dry_convert_input_bytes += iso_size
                    if result.would_delete_source:
                        dry_delete_count += 1
                        dry_delete_bytes += iso_size
                elif result.skipped_already_done:
                    dry_resume_skips += 1
                elif result.needs_attention:
                    dry_attention.append(
                        (iso_path, result.attention_reason or "needs attention", result.jre_missing)
                    )

    except KeyboardInterrupt:
        interrupted = True
        print()  # in case a live progress line was mid-write
        logger.warning("Interrupted by user (Ctrl+C) - stopping and printing the summary so far")

    if args.dry_run:
        def _rel(p: Path) -> str:
            try:
                return str(p.relative_to(input_root))
            except ValueError:
                return p.name

        free = free_bytes_on_volume(output_root)
        margin = args.free_space_margin_pct
        needed = int(dry_output_bytes * (1 + margin / 100.0))

        summary_lines = [
            "",
            "==================== DRY RUN PLAN ====================",
            f"Discs scanned      : {total_count} ({human_bytes(total_bytes_all)})",
            f"Would convert      : {dry_convert_count} disc(s) "
            f"({human_bytes(dry_convert_input_bytes)} in) -> {dry_titles_total} title(s)",
            f"Est. output size   : ~{human_bytes(dry_output_bytes)}",
            f"Already-converted  : {dry_resume_skips} (would be skipped by resume)",
            f"Needs attention    : {len(dry_attention)} disc(s)",
            "",
            "-- Space forecast (output volume) --",
        ]
        if free is None:
            summary_lines.append("  Free space  : unknown (could not read the output volume)")
        else:
            fits = free >= needed
            summary_lines.append(f"  Free space  : {human_bytes(free)}")
            summary_lines.append(
                f"  Est. needed : ~{human_bytes(needed)} (incl. {margin:g}% margin)  ->  "
                + ("OK, fits" if fits else f"SHORT by ~{human_bytes(needed - free)}")
            )
        if dry_attention:
            summary_lines.append("")
            summary_lines.append("-- Needs attention (resolve before a real run) --")
            for p, reason, _jre in dry_attention:
                summary_lines.append(f"  {_rel(p)}: {reason}")
            jre_n = sum(1 for _, _, jre in dry_attention if jre)
            if jre_n:
                summary_lines.append(
                    f"  ({jre_n} of these needed a JRE MakeMKV couldn't find - install a JRE or "
                    f"set app_Java in MakeMKV's settings.conf)"
                )
        if args.delete_source:
            summary_lines.append("")
            summary_lines.append("-- Deletion footprint --")
            summary_lines.append(
                f"  Would delete {dry_delete_count} source ISO(s) "
                f"(~{human_bytes(dry_delete_bytes)}) once each output verifies clean"
            )
        summary_lines.append("")
        summary_lines.append("Nothing was changed (dry run).")
        summary_lines.append("======================================================")
    else:
        summary_lines = [
            "",
            "==================== SUMMARY ====================",
            f"Successful title conversions : {stats.conversions_success}",
            f"Conversion errors            : {stats.conversions_error}",
            f"Conversion warnings          : {stats.warnings}",
            f"ISO files converted          : {stats.isos_converted}",
            f"Already-converted skipped    : {stats.already_converted_skipped}",
            f"Stalled (skipped, investigate): {stats.isos_stalled}",
            f"Total ISO bytes converted    : {human_bytes(stats.bytes_converted)}",
            "==================================================",
        ]
    for line in summary_lines:
        print(line)
        logger.append_raw_to_file(line)

    logger.close()

    # Non-zero exit lets an unattended run (cron, systemd timer, etc.)
    # actually be noticed as failed - previously this returned 0 even if
    # every single ISO in the batch failed to convert. Ctrl+C still wins
    # (130), matching normal shell conventions; a run with zero real
    # conversion errors (skips/warnings alone don't count) still exits 0.
    if interrupted:
        return 130
    return 1 if stats.conversions_error > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
