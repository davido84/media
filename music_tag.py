import mutagen
from mutagen.easyid3 import EasyID3
from pathlib import Path
import music_util
import music_filename
import logging


def tag_music_files_from_filename(args):
    for music_file in music_util.music_files(Path(args.input)):
        is_book = music_util.is_book(music_file)
        is_flac = music_file.suffix == '.flac'

        # For books, visit all files, else just flac
        if is_book or is_flac:
            disc, track, title = music_filename.title_info_from_filename(music_file)
            album = music_file.parent.name
            artist = music_file.parent.parent.name
            if is_book:
                title = f'Disc {disc}, Track {track}'

            info_message = (
                f'{music_file} : disc: {disc}, track: {track}, title: {title}, album: {album}, artist: {artist}')

            if not args.dry_run:
                file = mutagen.File(str(music_file)) if is_flac else EasyID3(str(music_file))
                file['artist'] = artist
                file['albumartist'] = artist
                file['album'] = album
                file['title'] = title
                file['tracknumber'] = str(track)
                file['discnumber'] = str(disc)
                if is_book:
                    file['author'] = artist
                file.save()
                info_message += ' UPDATED'

            logging.info(info_message)