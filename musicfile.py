import mutagen
from pathlib import Path
from mutagen.easyid3 import EasyID3
from music_filename import title_info_from_filename


class MusicFile:
    _ALBUM_TAG: str = 'album'
    _ARTIST_TAG: str = 'artist'
    _TITLE_TAG: str = 'title'
    _TRACK_NUMBER_TAG: str = 'tracknumber'
    _DISC_NUMBER_TAG: str = 'discnumber'

    def __init__(self, file: Path):
        self._file = file

        music_file = self._mutagen_file()

        self._title: [str, list[str]] = music_file.get(self._TITLE_TAG, '')
        self._artist: [str, list[str]] = music_file.get(self._ARTIST_TAG, '')
        self._album: [str, list[str]] = music_file.get(self._ALBUM_TAG, '')
        self._disc: [str] = music_file.get(self._DISC_NUMBER_TAG, '')
        self._track: [str] = music_file.get(self._TRACK_NUMBER_TAG, '')

    def __str__(self):
        return f'{self._artist}/{self._album} :"{self._title}" disc {self._disc}, track {self._track}'

    def _mutagen_file(self) -> [EasyID3, mutagen.File]:
        return mutagen.File(str(self._file)) if self._file.suffix == '.flac' else EasyID3(str(self._file))

    def delete_tags(self):
        self._title = ''
        self._artist = ''
        self._album = ''
        self._track = ''
        self._disc = ''
        self.save_tags()

    def reset_from_filename(self):
        disc, track, self._title = title_info_from_filename(self._file)
        self._disc = str(disc)
        self._track = str(track)
        self._album = self._file.parent.name
        self._artist = self._file.parent.parent.name
        if self.is_book:
            self._title = f'Disc {self._disc}, Track {self._track}'

    def save_tags(self):
        music_file = self._mutagen_file()
        music_file[self._ARTIST_TAG] = self._artist
        music_file[self._ALBUM_TAG] = self._album
        music_file[self._TITLE_TAG] = self._title
        music_file[self._DISC_NUMBER_TAG] = self._disc
        music_file[self._TRACK_NUMBER_TAG] = self._track
        music_file.save()

    @staticmethod
    def _query_str(value: [str, list[str]]) -> str:
        return value[0] if isinstance(value, list) and value else value

    @property
    def is_book(self) -> bool:
        return '_books' in str(self._file)

    @property
    def is_valid(self) -> bool:
        return all([self._title, self._artist, self._album, self._disc, self._track])

    @property
    def album(self) -> str:
        return self._query_str(self._album)

    @property
    def artist(self) -> str:
        return self._query_str(self._artist)

    @property
    def title(self) -> str:
        return self._query_str(self._title)

    @property
    def disc_number(self) -> int:
        return int(self._disc)

    @property
    def track_number(self) -> int:
        return int(self._track)