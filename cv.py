import argparse
from genericpath import isdir
import logging
from enum import Enum
import sys
import re
import shutil
from pathlib import Path
import os
import mediautil
from mediautil import IsoTitleInfo

class Command(Enum):
    MAKE = 'make' # Run handbrake on folder tree
    META = 'meta' # Create JSON meta files
    PREP = 'prep' # Create folder tree, fix filenames

class Settings:
    input_folder: Path
    output_folder: Path | None
    dry_run: bool
    force: bool

def action_meta(settings: Settings):
    print('Meta!')

def action_prep(settings: Settings):
    print('Preparing file names...')

    for iso_file in settings.input_folder.rglob("*.iso"):
        title_info = IsoTitleInfo(iso_file)

        print(f'{str(iso_file)} : {title_info}')

        # Lower-case iso
        mediautil.rename(iso_file, iso_file.with_suffix('.iso'), settings.dry_run)

        # Standardize season, episode
        if title_info.is_tv:
            mediautil.rename(iso_file,
                iso_file.with_stem(f'{title_info.season:02}-{title_info.disc:02}'),
                settings.dry_run)


def action_make(settings: Settings):
    print('Make!')
    
def main() -> int:
    parser = argparse.ArgumentParser(description='Process video.')
    parser.add_argument('--input', '-i', required=True, help='Input folder')
    parser.add_argument('--output', '-o', default=None, help='Output folder', required=False)
    parser.add_argument('--dry-run', '-y',action='store_true', default=False)
    parser.add_argument('--force', '-f', action='store_true', default=False)
   
    subparsers = parser.add_subparsers(title='Commands', description='Process video commands')

    parser_meta = subparsers.add_parser('meta')
    parser_meta.set_defaults(func=action_meta)

    parser_prep = subparsers.add_parser('prep')
    parser_prep.set_defaults(func=action_prep)

    args = parser.parse_args()

    settings = Settings()
    settings.input_folder = Path(args.input)
    settings.output_folder = Path if args.output else None
    settings.dry_run = args.dry_run
    settings.force = args.force

    args.func(settings)

    print('Finsihed!')
    return 0

if __name__ == "__main__":
    sys.exit(main())