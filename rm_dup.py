import music_util
import logging
from pathlib import Path
import media_util


def rm_dup(args):
    logging.info('Remove duplicates')

    files_removed: list[Path] = []
    bytes_deleted = 0

    def process_album(album_name: Path):
        logging.info(f'Processing album: {album_name}')
        album_file_dict: dict[int, Path] = {}
        for music_file in music_util.music_files(album):
            file_size = music_file.stat().st_size
            if file_size in album_file_dict:
                file_1 = music_file
                file_2 = album_file_dict[file_size]
                file_to_delete = file_1

                if '$' in file_1.stem and '$' not in file_2.stem:
                    file_to_delete = file_1
                elif '$' in file_2.stem and '$' not in file_1.stem:
                    file_to_delete = file_2
                elif len(file_1.stem) < len(file_2.stem):
                    file_to_delete = file_2
                elif len(file_2.stem) < len(file_1.stem):
                    file_to_delete = file_1
                else:
                    logging.error(
                        f'Could not determine duplicate file which should be deleted:\n{file_1}\n{file_2}')

                logging.info(f'Removing duplicate: {file_to_delete.stem}')

                nonlocal files_removed, bytes_deleted
                bytes_deleted += file_size
                files_removed.append(file_to_delete)

            else:
                album_file_dict[file_size] = music_file

    for album in music_util.album_folders(Path(args.input)):
        process_album(album)

    if not args.dry_run:
        print('Deleting files...')
        for file in files_removed:
            file.unlink()

    logging.info('Finished.')
    logging.info(f'Number of files removed: {len(files_removed)}')
    logging.info(f'Total bytes deleted: {media_util.gigabyte_string(bytes_deleted)}')