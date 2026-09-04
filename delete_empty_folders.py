#!/usr/bin/env python3
"""
delete_empty_folders.py

Recursively finds and deletes empty folders starting at a given input folder.

Usage:
    python delete_empty_folders.py /path/to/folder
    python delete_empty_folders.py /path/to/folder --dry-run
"""

import argparse
import os
import sys


def delete_empty_folders(root_folder, dry_run=False, verbose=True):
    """
    Recursively walk root_folder and delete any empty subfolders.

    Uses a bottom-up walk (topdown=False) so that folders which become
    empty after their (already-empty) subfolders are removed are also
    deleted in the same pass.

    Args:
        root_folder (str): Path to the folder to start searching from.
        dry_run (bool): If True, only report what would be deleted.
        verbose (bool): If True, print each folder as it's processed.

    Returns:
        int: Number of folders deleted (or that would be deleted, if dry_run).
    """
    deleted_count = 0

    # topdown=False -> os.walk visits subdirectories before their parents,
    # which lets a parent become empty (and thus deletable) after its
    # empty children have already been removed.
    for current_dir, subdirs, files in os.walk(root_folder, topdown=False):
        # A folder is "empty" if it has no files and no remaining subdirectories.
        # Because we're walking bottom-up, any subdirs that were empty and
        # got deleted already won't be here to block this check -- but
        # os.walk still reports the original subdirs list, so we verify
        # with os.listdir instead of trusting `subdirs`/`files` directly.
        try:
            if not os.listdir(current_dir):
                if dry_run:
                    print(f"[DRY RUN] Would delete: {current_dir}")
                else:
                    os.rmdir(current_dir)
                    if verbose:
                        print(f"Deleted: {current_dir}")
                deleted_count += 1
        except FileNotFoundError:
            # Folder may have already been removed as part of a parent cleanup.
            continue
        except OSError as e:
            print(f"Warning: could not delete '{current_dir}': {e}", file=sys.stderr)

    return deleted_count


def main():
    parser = argparse.ArgumentParser(
        description="Recursively delete empty folders starting at a given input folder."
    )
    parser.add_argument(
        "input_folder",
        help="Path to the folder to search recursively for empty subfolders.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which folders would be deleted without actually deleting them.",
    )
    parser.add_argument(
        "--include-root",
        action="store_true",
        help="Also delete the input folder itself if it ends up empty.",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress per-folder output.",
    )

    args = parser.parse_args()

    if not os.path.isdir(args.input_folder):
        print(f"Error: '{args.input_folder}' is not a valid directory.", file=sys.stderr)
        sys.exit(1)

    root = os.path.abspath(args.input_folder)

    count = delete_empty_folders(root, dry_run=args.dry_run, verbose=not args.quiet)

    # Optionally handle the root folder itself (os.walk's bottom-up pass
    # processes subfolders of root, but not root itself).
    if args.include_root:
        try:
            if not os.listdir(root):
                if args.dry_run:
                    print(f"[DRY RUN] Would delete root folder: {root}")
                else:
                    os.rmdir(root)
                    if not args.quiet:
                        print(f"Deleted root folder: {root}")
                count += 1
        except OSError as e:
            print(f"Warning: could not delete root folder '{root}': {e}", file=sys.stderr)

    action = "Would delete" if args.dry_run else "Deleted"
    print(f"\n{action} {count} empty folder(s).")


if __name__ == "__main__":
    main()
