import os
import music_util
import logging
from pathlib import Path
import media_util
import subprocess


def _checksum(file: Path) -> str:
    output = subprocess.run([
        'ffmpeg', '-loglevel', 'error', '-i', str(file), '-map', '0', '-f', 'hash', '-hash', 'md5', '-'],
                            capture_output=True, check=True)
    
    return output.stdout.decode().strip()[4:]


def show_duplicates(args) -> None:
    file_size_dict: dict[int, Path] = {}
    albums = [A for A in music_util.album_folders(Path(args.input))]
    artists = set([A.parent for A in albums])
    files_to_remove: set[Path] = set()
    bytes_removed = 0

    for file in music_util.music_files(Path(args.input)):
        if '_books' in str(file):
            continue

        file_size = file.stat().st_size
        if file_size in file_size_dict:
            file_1 = file
            file_2 = file_size_dict[file_size]
            file_1_is_classical = '_classical' in str(file_1)
            file_2_is_classical = '_classical' in str(file_2)

            file_to_remove: Path | None = None
            if file_1_is_classical != file_2_is_classical:
                file_to_remove = file_1 if file_2_is_classical else file_2
            else:
                file_to_remove = file_1 if len(str(file_1)) > len(str(file_2)) else file_2
                
            logging.warning(f'Duplicate: {file_to_remove}')
            files_to_remove.add(file_to_remove)
            bytes_removed += file_to_remove.stat().st_size

        else:
            file_size_dict[file_size] = file

    if not args.dry_run:
        for file in files_to_remove:
            file.unlink()
            logging.info(f'Deleted {file}')

        for album in [A for A in albums if len(os.listdir(str(A))) == 0]:
            logging.info(f'Removed empty folder: {album}')
            os.rmdir(str(album))
        for artist in [A for A in artists if len(os.listdir(str(A))) == 0]:
            logging.info(f'Removed empty folder: {artist}')
            os.rmdir(str(artist))

    logging.info(f'{len(files_to_remove)} duplicates found.')
    logging.info(f'Total removed: {media_util.gigabyte_string(bytes_removed)}')


def rm_dup(args):
    logging.info('Remove duplicates started.')

    files_removed: list[Path] = []
    bytes_deleted = 0

    def process_album(album_name: Path):
        logging.info(f'Processing album: {album_name}')
        album_file_dict: dict[int, Path] = {}
        assert not album_file_dict
        for music_file in music_util.music_files(album):
            file_size = music_file.stat().st_size
            if file_size in album_file_dict and _checksum(music_file) == _checksum(album_file_dict[file_size]):
                file_1 = music_file
                file_2 = album_file_dict[file_size]

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
                        f'Could not determine duplicate file which should be deleted:\n{file_1} -- {file_2}')
                    raise media_util.MediaException

                if args.dry_run:
                    logging.info(f'Found duplicate: "{file_to_delete.stem}"')

                nonlocal files_removed, bytes_deleted
                bytes_deleted += file_size
                files_removed.append(file_to_delete)
                if not args.dry_run:
                    file_to_delete.unlink()
                    logging.info(f'Deleted: "{file_to_delete}"')
            else:
                album_file_dict[file_size] = music_file

    try:
        for album in music_util.album_folders(Path(args.input)):
            process_album(album)

    except media_util.MediaException:
        pass

    logging.info(f'Number of files deleted: {len(files_removed)}')
    logging.info(f'Total bytes deleted: {media_util.gigabyte_string(bytes_deleted)}')
    logging.info('Finished.')