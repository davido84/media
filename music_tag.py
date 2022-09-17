import mutagen
from mutagen.easyid3 import EasyID3
from pathlib import Path
import music_util
import music_filename
import logging


def tag_music_files(args):
    for music_file in music_util.music_files(Path(args.input)):
        is_book = music_util.is_book(music_file)
        if is_book or args.force:
            disc, track, title = music_filename.title_info(music_file)
            album = music_file.parent.name
            artist = music_file.parent.parent.name
            if is_book:
                title = f'Disc {disc}, Track {track}'
            logging.info(
                f'{music_file} : disc: {disc}, track: {track}, title: {title}, album: {album}, artist: {artist}')
            if not args.dry_run:
                if music_file.suffix == '.mp3':
                    file = EasyID3(str(music_file))
                else:
                    file = mutagen.File(str(music_file))

                file['artist'] = artist
                file['albumartist'] = artist
                file['album'] = album
                file['title'] = title
                file['tracknumber'] = str(track)
                file['discnumber'] = str(disc)
                file.save()