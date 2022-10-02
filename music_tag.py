from pathlib import Path
import music_util
from musicfile import MusicFile
import logging


def tag_music_files_from_filename(args):
    for file in music_util.music_files(Path(args.input)):
        music_file = MusicFile(file)
        if args.clear:
            music_file.clear_tags()
        else:
            music_file.reset_from_filename()

        music_file.save_tags(args)