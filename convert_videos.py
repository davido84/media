#!/usr/bin/env python3
"""
Batch-convert .mp4 files in a folder (recursively) to H.265/HEVC using ffmpeg,
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
import shutil
import subprocess
import sys
import time
from pathlib import Path


class ConversionError(Exception):
    """Raised when ffprobe or ffmpeg fails for a given file."""
    def __init__(self, file: Path, reason: str):
        self.file = file
        self.reason = reason
        super().__init__(f"{file}: {reason}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Recursively re-encode .mp4 files to H.265 (software), "
                    "downscaling to 1080p if larger, skipping files already in H.265."
    )
    parser.add_argument("-i", "--input", dest="input_folder", type=Path, default=Path("."),
                         help="Folder to scan recursively for .mp4 files. Default: current folder")
    parser.add_argument("-o", "--output", dest="output_folder", type=Path, default=None,
                         help="Folder to write converted/copied files to. Default: current folder. "
                              "If this is the same as the input folder, converted files replace "
                              "the originals in place. Required when --duration is set (test encodes "
                              "must not overwrite your source files).")
    parser.add_argument("--crf", type=int, default=22,
                         help="x265 CRF value (lower = higher quality/larger file). Default: 22")
    parser.add_argument("--duration", type=float, default=-1,
                         help="Encode only the first N seconds of each file. "
                              "Default: -1 (encode the full file)")
    parser.add_argument("--dry-run", action="store_true",
                         help="List what would be done for each file without actually "
                              "encoding or copying anything.")
    parser.add_argument("--min-size-mb", type=float, default=100,
                         help="Files smaller than this size (in MB) are skipped and just "
                              "copied to the output folder as-is. Default: 100")
    parser.add_argument("--encoding", choices=["hardware", "software"], default="software",
                         help="Encoding mode. 'software' uses libx265 (CPU). 'hardware' uses "
                              "Intel Quick Sync (hevc_qsv). Default: software")
    parser.add_argument("--normalize-audio", action="store_true",
                         help="Apply EBU R128 loudness normalization (ffmpeg's loudnorm filter) "
                              "to the audio track during encoding. Only affects files that are "
                              "actually re-encoded — files copied as-is (already H.265, or below "
                              "--min-size-mb) are left untouched. Default: off")
    parser.add_argument("--loudnorm-target", type=float, default=-16,
                         help="Integrated loudness target in LUFS, used with --normalize-audio. "
                              "-16 is typical for general/streaming content, -23 is the EBU "
                              "broadcast standard. Default: -16")
    parser.add_argument("-f", "--force", action="store_true",
                         help="Overwrite output files that already exist. Without this flag, "
                              "if a file already exists at the output path, it is silently "
                              "skipped.")
    parser.add_argument("--limit", type=float, default=-1,
                         help="Stop processing once this many GB of files have been "
                              "converted or copied (cumulative original size). "
                              "Default: -1 (no limit)")
    return parser.parse_args()


def setup_logging(output_folder: Path) -> Path:
    output_folder.mkdir(parents=True, exist_ok=True)
    log_path = output_folder / "conversion_log.txt"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_path


def probe_video(path: Path) -> dict:
    """Return codec_name, width, height, and duration (seconds, float) for the video.
    Raises ConversionError on failure."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height:format=duration",
        "-of", "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            raise ConversionError(path, "no video stream found")
        info = dict(streams[0])
        duration_str = data.get("format", {}).get("duration")
        try:
            info["duration"] = float(duration_str) if duration_str is not None else None
        except ValueError:
            info["duration"] = None
        return info
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError) as e:
        raise ConversionError(path, f"ffprobe failed: {e}")


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def build_ffmpeg_cmd(src: Path, dst: Path, crf: int, duration: float, needs_downscale: bool,
                      encoding: str = "software", normalize_audio: bool = False,
                      loudnorm_target: float = -16) -> list:
    cmd = ["ffmpeg", "-y", "-i", str(src)]

    if duration != -1:
        cmd += ["-t", str(duration)]

    if needs_downscale:
        # Scale down so neither dimension exceeds 1080p, preserving aspect ratio.
        # force_original_aspect_ratio=decrease only shrinks, never upscales.
        # force_divisible_by=2 rounds the calculated dimension to an even number,
        # since x265/x264 require even width & height for 4:2:0 chroma subsampling.
        cmd += ["-vf", "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2"]

    if encoding == "hardware":
        # Intel Quick Sync HEVC encoder. QSV uses -global_quality as its CRF-equivalent
        # quality knob rather than -crf.
        cmd += ["-c:v", "hevc_qsv", "-global_quality", str(crf), "-preset", "slow"]
    else:
        cmd += ["-c:v", "libx265", "-preset", "slow", "-crf", str(crf)]

    cmd += ["-c:a", "libopus", "-b:a", "128k"]

    if normalize_audio:
        # One-pass EBU R128 loudness normalization. TP/LRA use sensible general-purpose
        # defaults; only the integrated loudness target (I) is user-configurable.
        cmd += ["-af", f"loudnorm=I={loudnorm_target}:TP=-1.5:LRA=11"]

    cmd += [str(dst)]
    return cmd


def process_file(src: Path, dst: Path, crf: int, duration: float, min_size_mb: float,
                  same_location: bool, dry_run: bool = False, encoding: str = "software",
                  normalize_audio: bool = False, loudnorm_target: float = -16):
    """Returns (original_size, new_size, video_duration_seconds, action, downscaled) on
    success, or None if skipped/dry-run. action is 'encoded' or 'copied'. downscaled is
    True if the file was scaled down from >1080p. video_duration_seconds is None if the
    file was small enough to skip probing entirely. Raises ConversionError if ffprobe or
    ffmpeg fails."""
    min_size_bytes = min_size_mb * 1024 * 1024
    src_size = src.stat().st_size

    if src_size < min_size_bytes:
        if same_location:
            logging.info(f"KEPT (below {min_size_mb}MB minimum, already in place, "
                         f"{human_size(src_size)}): {src}")
            return (src_size, src_size, None, "copied", False)
        if dry_run:
            logging.info(f"[DRY RUN] WOULD COPY (below {min_size_mb}MB minimum, "
                         f"{human_size(src_size)}): {src} -> {dst}")
            return None
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logging.info(f"COPIED (below {min_size_mb}MB minimum, "
                     f"{human_size(src_size)}): {src} -> {dst}")
        return (src_size, src_size, None, "copied", False)

    info = probe_video(src)
    codec = info.get("codec_name", "")
    width = info.get("width", 0)
    height = info.get("height", 0)
    video_duration = info.get("duration")

    if codec == "hevc":
        if same_location:
            logging.info(f"KEPT (already H.265, already in place): {src}")
            return (src_size, src_size, video_duration, "copied", False)
        if dry_run:
            logging.info(f"[DRY RUN] WOULD COPY (already H.265): {src} -> {dst}")
            return None
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logging.info(f"COPIED (already H.265): {src} -> {dst}")
        return (src_size, src_size, video_duration, "copied", False)

    needs_downscale = width > 1920 or height > 1080

    log_action = "[DRY RUN] WOULD ENCODE" if dry_run else "ENCODING"
    logging.info(f"{log_action}: {src} -> {dst} (codec={codec}, {width}x{height}, "
                 f"downscale={'yes' if needs_downscale else 'no'}, crf={crf}, "
                 f"encoding={encoding}, "
                 f"normalize_audio={'yes (' + str(loudnorm_target) + ' LUFS)' if normalize_audio else 'no'}, "
                 f"duration={'full' if duration == -1 else f'{duration}s'})")

    if dry_run:
        cmd = build_ffmpeg_cmd(src, dst, crf, duration, needs_downscale, encoding,
                                normalize_audio, loudnorm_target)
        logging.info(f"[DRY RUN] Command: {' '.join(cmd)}")
        return None

    # When replacing in place, ffmpeg can't read and write the same path at once,
    # so encode to a temp file alongside it, then swap it in on success.
    if same_location:
        encode_target = dst.parent / f".{dst.name}.converting.tmp"
    else:
        encode_target = dst

    encode_target.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_ffmpeg_cmd(src, encode_target, crf, duration, needs_downscale, encoding,
                            normalize_audio, loudnorm_target)
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        if encode_target.exists():
            encode_target.unlink(missing_ok=True)
        raise ConversionError(src, f"ffmpeg exited with code {result.returncode}: "
                                    f"{result.stderr[-2000:]}")

    if same_location:
        os.replace(encode_target, dst)  # dst == src here; atomic swap-in

    orig_size = src_size
    new_size = dst.stat().st_size if dst.exists() else 0
    saved_pct = (1 - new_size / orig_size) * 100 if orig_size else 0
    logging.info(f"DONE: {src} -> {dst} "
                 f"({human_size(orig_size)} -> {human_size(new_size)}, {saved_pct:.1f}% smaller)")
    return (orig_size, new_size, video_duration, "encoded", needs_downscale)


def main():
    args = parse_args()

    if not args.input_folder.is_dir():
        print(f"Input folder does not exist: {args.input_folder}", file=sys.stderr)
        sys.exit(1)

    if args.duration != -1 and args.output_folder is None:
        print("Error: -o/--output is required when --duration is set. "
              "Test encodes must be written to a separate folder so your source "
              "files are never overwritten with a partial encode.", file=sys.stderr)
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

    log_path = setup_logging(args.output_folder)
    mode = "DRY RUN" if args.dry_run else "LIVE"
    logging.info(f"Starting batch conversion [{mode}]. Input: {input_resolved} "
                 f"Output: {output_resolved} "
                 f"({'in-place' if same_location else 'separate output'}) CRF: {args.crf} "
                 f"Encoding: {args.encoding} "
                 f"Normalize audio: {'yes (' + str(args.loudnorm_target) + ' LUFS)' if args.normalize_audio else 'no'} "
                 f"Duration limit: {'none' if args.duration == -1 else f'{args.duration}s'} "
                 f"Min size: {args.min_size_mb}MB "
                 f"Data limit: {'none' if args.limit == -1 else f'{args.limit}GB'}")

    mp4_files = sorted(args.input_folder.rglob("*.mp4"))
    logging.info(f"Found {len(mp4_files)} .mp4 file(s) to process.")

    total_orig = 0
    total_new = 0
    total_duration_seconds = 0.0
    failed_files = []
    skipped_existing = 0
    encoded_count = 0
    copied_count = 0
    downscaled_count = 0
    limit_bytes = float("inf") if args.limit == -1 else args.limit * 1024 ** 3
    limit_reached = False

    total_files = len(mp4_files)
    total_size_bytes = sum(f.stat().st_size for f in mp4_files)
    processed_bytes = 0
    batch_start = time.monotonic()

    for index, src in enumerate(mp4_files, start=1):
        src_size = src.stat().st_size
        remaining_bytes = total_size_bytes - processed_bytes

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
                                   args.normalize_audio, args.loudnorm_target)
        except ConversionError as e:
            failed_path = e.file.resolve()
            logging.error(f"CONVERSION FAILED: {failed_path}\n{e.reason}")
            failed_files.append(failed_path)
            processed_bytes += src_size
            continue

        processed_bytes += src_size

        if result is not None:
            orig_size, new_size, video_duration, action, downscaled = result
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

            if total_orig >= limit_bytes:
                limit_reached = True
                logging.info(f"Data limit of {args.limit}GB reached "
                             f"({human_size(total_orig)} processed). Stopping.")
                print(f"\nData limit of {args.limit}GB reached "
                     f"({human_size(total_orig)} processed). Stopping.")
                break

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

    if args.dry_run:
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
        print(f"Log written to: {log_path}")

    final_line = f"Finished with {len(failed_files)} error(s)."
    logging.info(final_line)
    print(final_line)


if __name__ == "__main__":
    main()
