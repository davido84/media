from pathlib import Path
import music_util
from musicfile import MusicFile
import logging


def tag_music_files_from_filename(args):
    for file in music_util.music_files(Path(args.input)):
        music_file = MusicFile(file)
        if args.delete:
            if not args.dry_run:
                music_file.delete_tags()
            logging.info(f'Deleted tags for {file}')
        else:
            music_file.reset_from_filename()
            info_message = f'{music_file}'
            if not args.dry_run:
                music_file.save_tags()
                info_message += ' UPDATED'

            logging.info(info_message)