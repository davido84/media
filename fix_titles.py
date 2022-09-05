import music_util
import music_filename
from pathlib import Path
import logging
import os


def fix_titles(args):
    logging.info('Fix Titles')
    logging.info(f'Dry run: {args.dry_run}')

    matched_files = 0
    unmatched_files = 0
    validated_files = 0
    renamed_files = 0

    for file in music_util.music_files(Path(args.input)):
        if music_filename.validate(file):
            validated_files += 1
        elif new_name := music_filename.fix(file):
            logging.info(f'Matched:\n{file}\n{new_name}\n')
            matched_files += 1
            if not args.dry_run:
                os.rename(str(file), str(new_name))
                renamed_files += 1
        else:
            logging.warning(f'Unmatched: {file}')
            unmatched_files += 1

    logging.info('Finished.')
    logging.info(f'Validated: {validated_files:,}')
    logging.info(f'Matched: {matched_files:,}')
    logging.info(f'Unmatched: {unmatched_files:,}')
    logging.info(f'Renamed: {renamed_files:,}')