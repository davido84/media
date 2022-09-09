import sys
import music_util
import logging
from pathlib import Path
import media_util
import subprocess


def _checksum(file: Path) -> str:
    output = subprocess.run([
        'ffmpeg', '-loglevel', 'error', '-i', str(file), '-map', '0', '-f', 'hash', '-hash', 'md5', '-'],
                            capture_output=True, check=True)
    
    return output.stdout.decode().strip()


def rm_dup(args):
    logging.info('Remove duplicates')

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

                logging.info(f'Removing duplicate: {file_to_delete.stem}')

                nonlocal files_removed, bytes_deleted
                bytes_deleted += file_size
                files_removed.append(file_to_delete)

            else:
                album_file_dict[file_size] = music_file

    try:
        for album in music_util.album_folders(Path(args.input)):
            process_album(album)

        if not args.dry_run:
            print('Deleting files...')
            for file in files_removed:
                file.unlink()
                sys.stderr.write('.')

    except media_util.MediaException:
        pass

    logging.info(f'Number of files deleted: {len(files_removed)}')
    logging.info(f'Total bytes deleted: {media_util.gigabyte_string(bytes_deleted)}')
    logging.info('Finished.')
