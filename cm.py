import argparse
import logging
from enum import Enum
import sys
from pathlib import Path
from subprocess import CalledProcessError

import mediautil
from mediautil import IsoTitleInfo, run_handbrake, rename_file
from datetime import datetime
import re

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL
}

logger = mediautil.get_logger()

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
    try:
        for media_file in media_files(settings):
            output_file: Path = Path(media_file.with_suffix('.json'))
            if output_file.exists() and not settings.force:
                continue

            logger.info(str(media_file))
            result = run_handbrake(media_file, ['--json', '--scan', '--main-feature'])
            json: str = result.stdout.split('JSON Title Set:')[1]

            if not settings.dry_run:
                output_file.write_text(json)

    except CalledProcessError:
        logger.error('Error running Handbrake')
    except OSError:
        logger.error('Error writing file')
    except IndexError:
        logger.error('Output is missing JSON section')

def check_iso_warnings(settings: Settings, media_file: Path, title_info: IsoTitleInfo) -> None:
    # Check to see tha we are no more than 1 folder below the input folder
    if media_file.parent != settings.input_folder and media_file.parent.parent != settings.input_folder:
        logger.warning(f'"{media_file}" Folder depth is more than one')

    if '{' in title_info.title or '}' in title_info.title:
        logger.warning(f'{title_info.title} : possible invalid title')

    if title_info.season is None and title_info.tvdb is not None:
        logger.warning(f'"{title_info.title}" contains tvdb but no season/disc')
    if title_info.tvdb is not None and not title_info.is_tv():
        logger.warning(f'"{title_info.title}" contains tvdb but not identified as TV')

    if title_info.disc is not None and title_info.disc < 1:
        logger.error(f'{media_file} : Invalid disc number')

    if title_info.season is not None and title_info.season < 1:
        logger.error(f'{media_file} : Invalid season number')

    if title_info.year is not None and title_info.year < 1933:
        logger.error(f'{title_info.title} possible invalid year')

def action_prep(settings: Settings):
    logger.info('PREP - Preparing file names...')

    for media_file in settings.input_folder.rglob("*.iso"):
        if '_exclude' in str(media_file):
            continue

        title_info = IsoTitleInfo(media_file, settings.input_folder)

        if not title_info.title:
            logger.error('Missing title')
            continue

        logger.info(f'{title_info} : "{str(media_file)}"')
        check_iso_warnings(settings, media_file, title_info)

        # Lower-case iso
        final_file = rename_file(media_file, media_file.with_suffix('.iso'), settings.dry_run)
       
        # Standardize season, episode
        if title_info.season is not None and title_info.disc is not None:
            rename_file(final_file,
                final_file.with_stem(f'{title_info.season:02}-{title_info.disc:02}'), settings.dry_run)
        elif title_info.disc is not None:
            rename_file(final_file,
                final_file.with_stem(f'{title_info.disc:02}'), settings.dry_run)

def action_make(settings: Settings):
    print('Make!')

def media_files(settings: Settings) -> list[Path]:
    files : list[Path] = []
    for pattern in ['*.iso', '*.mkv']:
        files.extend(settings.input_folder.rglob(pattern))

    return files

def main() -> int:
    parser = argparse.ArgumentParser(description='Process video.')
    parser.add_argument('--input', '-i', required=True, help='Input folder')
    parser.add_argument('--output', '-o', default=None, help='Output folder', required=False)
    parser.add_argument('--dry-run', '-y',action='store_true', default=False,
                        help='Do not write any output or change any files.')
    parser.add_argument('--force', '-f', action='store_true', default=False,
                        help='Overwrite existing output files.')
    parser.add_argument('--logfile', type=str, default=None, help='Log file name')
    parser.add_argument("--loglevel", type=str, default='info', choices=_LOG_LEVELS.keys(),
        help="Set the logging level. Options: debug, info, warning, error, critical.")
   
    subparsers = parser.add_subparsers(title='Commands', description='Process video commands')

    parser_meta = subparsers.add_parser('meta', help='Create JSON files for ISO images')
    parser_meta.set_defaults(func=action_meta)

    parser_prep = subparsers.add_parser('prep', help='Clean up file names and check filenames for errors.')
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

    time_format = "%B %d %I:%M:%S %p"
    logger.info(f'Convert video started: {datetime.now().strftime(time_format)}')

    settings = Settings()
    settings.input_folder = Path(args.input)
    settings.output_folder = Path if args.output else None
    settings.dry_run = args.dry_run
    settings.force = args.force

    if settings.dry_run:
        logger.info('DRY RUN')

    try:
        args.func(settings)
    except AttributeError:
        print('Error: missing command')
        return -1

    logger.info(f'Convert video finished: {datetime.now().strftime(time_format)}')
    return 0

if __name__ == "__main__":
    sys.exit(main())