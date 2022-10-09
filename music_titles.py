import music_util
import music_filename
from pathlib import Path
import logging
import os
from functools import partial
import concurrent
import concurrent.futures
from musicfile import MusicFile


def validate_metadata(args):
    file_count = 0

    def do_validate(input_file: Path) -> [Path, bool]:
        nonlocal file_count
        if file_count % 1000 == 0:
            print('.', end='', flush=True)

        file_count += 1
        return input_file, MusicFile(input_file).is_valid

    num_errors = 0
    num_valid = 0

    with concurrent.futures.ThreadPoolExecutor(os.cpu_count()) as tp:
        workers = [
            tp.submit(partial(do_validate), T) for T in music_util.music_files(Path(args.input), ['flac', 'mp3'])]
        for worker in concurrent.futures.as_completed(workers):
            file, result = worker.result()
            if result:
                num_valid += 1
            else:
                num_errors += 1

        print('')
        logging.info(f'{num_errors:,} error(s) found.')
        logging.info(f'{num_valid:,} valid file(s).')


def process_titles(args):

    matched_files = 0
    unmatched_files: list[Path] = []
    validated_files = 0
    renamed_files = 0
    do_rename = not args.dry_run and not args.validate

    for file in music_util.music_files(Path(args.input)):
        if music_filename.validate_filename(file):
            validated_files += 1
        elif new_name := music_filename.fix(file):
            logging.info(f'Matched:{file} --> {new_name.stem}')
            matched_files += 1
            if not do_rename:
                if new_name.exists():
                    file.unlink()
                else:
                    os.rename(str(file), str(new_name))
                    renamed_files += 1
        else:
            logging.warning(f'Unmatched: {file}')
            unmatched_files.append(file)

    logging.info(f'Validated: {validated_files:,}')
    logging.info(f'Matched: {matched_files:,}')
    logging.info(f'Unmatched: {len(unmatched_files):,}')

    if do_rename:
        logging.info(f'Renamed: {renamed_files:,}')
    else:
        if matched_files == 0 and not unmatched_files:
            logging.info('All files validated.')