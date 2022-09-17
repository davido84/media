import music_util
import music_filename
from pathlib import Path
import logging
import os
from functools import partial
import concurrent
import concurrent.futures
import mutagen


class MusicFile:
    def __init__(self, file_name: Path):
        music_file = mutagen.File(str(file_name))
        self._title: str | None = music_file.get('title', None)
        self._artist: str | None = music_file.get('artist', None)
        self._album: str | None = music_file.get('album', None)
        self._disc: str | None = music_file.get('discnumber', None)
        self._track: str | None = music_file.get('tracknumber', None)

    @property
    def is_valid(self) -> bool:
        return all([self._title, self._artist, self._album, self._disc, self._track])


def validate_metadata(args):
    def do_validate(input_file: Path) -> [Path, bool]:
        print('.', end='')
        return input_file, MusicFile(input_file).is_valid

    num_errors = 0
    num_valid = 0

    with concurrent.futures.ThreadPoolExecutor(os.cpu_count()) as tp:
        workers = [tp.submit(partial(do_validate), T) for T in music_util.music_files(Path(args.input), ['flac', 'mp3'])]
        for worker in concurrent.futures.as_completed(workers):
            file, result = worker.result()
            if result:
                num_valid += 1
            else:
                logging.warning(f'{file}')
                num_errors += 1

        print('')
        logging.info(f'{num_errors} error(s) found.')
        logging.info(f'{num_valid} valid file(s).')


def fix_titles(args):
    logging.info(f'Dry run: {args.dry_run}')

    matched_files = 0
    unmatched_files: list[Path] = []
    validated_files = 0
    renamed_files = 0

    for file in music_util.music_files(Path(args.input)):
        if music_filename.validate(file):
            validated_files += 1
        elif new_name := music_filename.fix(file):
            logging.info(f'Matched:{file} --> {new_name.stem}')
            matched_files += 1
            if not args.dry_run:
                if new_name.exists():
                    file.unlink()
                else:
                    os.rename(str(file), str(new_name))
                    renamed_files += 1
        else:
            logging.warning(f'Unmatched: {file}')
            unmatched_files.append(file)

    logging.info('Finished.')
    logging.info(f'Validated: {validated_files:,}')
    logging.info(f'Matched: {matched_files:,}')
    logging.info(f'Unmatched: {len(unmatched_files):,}')
    logging.info(f'Renamed: {renamed_files:,}')