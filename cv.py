import argparse
import logging
from enum import Enum
import sys
import re
import shutil
from pathlib import Path
import os
import mediautil
from mediautil import IsoTitleInfo, MediaType

logger = logging.getLogger('ConvertMedia')

def rename_file(file: Path, new_name: Path, dry_run: bool) ->None:
    if str(file) != str(new_name):
        if dry_run:
            logger.debug(f'[RENAME] "{str(file)}" -> "{new_name}"')
        else:
            file.rename(new_name)

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
        title_info = IsoTitleInfo(iso_file, settings.input_folder)

        logger.info(f'{title_info} : "{str(iso_file)}"')
        # Check to see tha we are no more than 1 folder below the input folder
        if iso_file.parent != settings.input_folder and iso_file.parent.parent != settings.input_folder:
            logger.warning(f'"{title_info.title}" Folder depth is more than one')

        # if all([not title_info.imdb, not title_info.tvdb, not title_info.year]):
        #    logger.warning('Missing year')
        
        # Check for TV
        if title_info.season is None and title_info.tvdb is not None:
            logger.warning(f'"{title_info.title}" contains tvdb but no season/disc')
        if title_info.tvdb is not None and not title_info.is_tv():
            logger.warning(f'"{title_info.title}" contains tvdb but not identified as TV')

        # Lower-case iso
        rename_file(iso_file, iso_file.with_suffix('.iso'), settings.dry_run)
       
        # Standardize season, episode
        if title_info.season is not None and title_info.disc is not None:
            rename_file(iso_file,
                iso_file.with_stem(f'{title_info.season:02}-{title_info.disc:02}'), settings.dry_run)
        elif title_info.disc is not None:
            rename_file(iso_file,
                iso_file.with_stem(f'{title_info.disc:02}'), settings.dry_run)


def action_make(settings: Settings):
    print('Make!')
    
def main() -> int:
    # logging.basicConfig(filename='myapp.log', level=logging.INFO)

    # logger.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

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

    logger.setLevel(logging.INFO)
    # Create handlers
    console_handler = logging.StreamHandler()  # Log to the console
    # file_handler = logging.FileHandler("logfile.log")  # Log to a file
    console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    # file_handler.setFormatter(formatter)
    # Add handlers to the logger
    logger.addHandler(console_handler)
    # logger.addHandler(file_handler)


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