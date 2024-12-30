import argparse
import logging
from enum import Enum
import sys
import re
import shutil
from pathlib import Path
from mediautil import IsoTitleInfo
from datetime import datetime

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL
}

logger = logging.getLogger('ConvertMedia')

def rename_file(file: Path, new_name: Path, dry_run: bool) -> Path:
    if str(file) != str(new_name):
        info = f'[RENAME] "{str(file)}" -> "{new_name}"'
        if dry_run:
            logger.info(f'[RENAME dry run] {info}')
        else:
            try:
                logger.info(f'Renamed file {info}')
                return file.rename(new_name)
            except OSError:
                logger.critical(f'Error renaming: {str(file)}')

    return new_name

class Command(Enum):
    MAKE = 'make' # Run handbrake on folder tree
    META = 'meta' # Create JSON meta files
    PREP = 'prep' # Create folder tree, fix filenames
    TEST = 'test' # Test handbrake

class Settings:
    input_folder: Path
    output_folder: Path | None
    dry_run: bool
    force: bool

def action_meta(settings: Settings):
    print('Meta!')

def check_iso_warnings(settings: Settings, iso_file: Path, title_info: IsoTitleInfo) -> None:
    # Check to see tha we are no more than 1 folder below the input folder
    if iso_file.parent != settings.input_folder and iso_file.parent.parent != settings.input_folder:
        logger.warning(f'"{title_info.title}" Folder depth is more than one')

    if '{' in title_info.title or '}' in title_info.title:
        logger.warning(f'{title_info.title} : possible invalid title')

    if title_info.season is None and title_info.tvdb is not None:
        logger.warning(f'"{title_info.title}" contains tvdb but no season/disc')
    if title_info.tvdb is not None and not title_info.is_tv():
        logger.warning(f'"{title_info.title}" contains tvdb but not identified as TV')

    if title_info.disc is not None and title_info.disc < 1:
        logger.error(f'{iso_file} : Invalid disc number')

    if title_info.season is not None and title_info.season < 1:
        logger.error(f'{iso_file} : Invalid season number')

    if title_info.year is not None and title_info.year < 1933:
        logger.error(f'{title_info.title} possible invalid year')

def action_prep(settings: Settings):
    print('Preparing file names...')

    for iso_file in settings.input_folder.rglob("*.iso"):
        if '_exclude' in str(iso_file):
            continue

        title_info = IsoTitleInfo(iso_file, settings.input_folder)

        if not title_info.title:
            logger.error('Missing title')
            continue

        logger.debug(f'{title_info} : "{str(iso_file)}"')
        check_iso_warnings(settings, iso_file, title_info)

        # Lower-case iso
        final_file = rename_file(iso_file, iso_file.with_suffix('.iso'), settings.dry_run)
       
        # Standardize season, episode
        if title_info.season is not None and title_info.disc is not None:
            rename_file(final_file,
                final_file.with_stem(f'{title_info.season:02}-{title_info.disc:02}'), settings.dry_run)
        elif title_info.disc is not None:
            rename_file(final_file,
                final_file.with_stem(f'{title_info.disc:02}'), settings.dry_run)

def action_make(settings: Settings):
    print('Make!')
    
def main() -> int:
    parser = argparse.ArgumentParser(description='Process video.')
    parser.add_argument('--input', '-i', required=True, help='Input folder')
    parser.add_argument('--output', '-o', default=None, help='Output folder', required=False)
    parser.add_argument('--dry-run', '-y',action='store_true', default=False)
    parser.add_argument('--force', '-f', action='store_true', default=False)
    parser.add_argument('--logfile', type=str, default=None)
    parser.add_argument("--loglevel", type=str, default="info", choices=_LOG_LEVELS.keys(),
        help="Set the logging level. Options: debug, info, warning, error, critical.")
   
    subparsers = parser.add_subparsers(title='Commands', description='Process video commands')

    parser_meta = subparsers.add_parser('meta')
    parser_meta.set_defaults(func=action_meta)

    parser_prep = subparsers.add_parser('prep')
    parser_prep.set_defaults(func=action_prep)

    args = parser.parse_args()
    logger.setLevel(_LOG_LEVELS[args.loglevel])

    # Create handlers
    console_handler = logging.StreamHandler()  # Log to the console
    formatter = logging.Formatter('%(levelname)s: %(message)s')
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if args.logfile:
        file_handler = logging.FileHandler(args.logfile, mode='w')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.info(f'Convert video started {datetime.now().strftime("%m-%d %H:%M:%S")}')

    settings = Settings()
    settings.input_folder = Path(args.input)
    settings.output_folder = Path if args.output else None
    settings.dry_run = args.dry_run
    settings.force = args.force

    try:
        args.func(settings)
    except AttributeError:
        print('Error: missing command')
        return -1

    logger.info(f'Convert video finished {datetime.now().strftime("%m-%d %H:%M:%S")}')
    return 0

if __name__ == "__main__":
    sys.exit(main())