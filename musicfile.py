import logging

import mutagen
from pathlib import Path
from mutagen.easyid3 import EasyID3
from music_filename import title_info_from_filename


class MusicFile:
    _ALBUM_TAG: str = 'album'
    _ARTIST_TAG: str = 'artist'
    _TITLE_TAG: str = 'title'
    _TRACK_NUMBER_TAG: str = 'track_number'
    _DISC_NUMBER_TAG: str = 'disc_number'
    _ALBUM_ARTIST_TAG: str = 'album_artist'

    def __init__(self, file: Path):
        self._file = file
        self._is_modified = False
        self._music_file = mutagen.File(str(self._file)) if self._file.suffix == '.flac' else EasyID3(str(self._file))

    def _required_tags(self) -> list[str]:
        return [self._DISC_NUMBER_TAG,
                self._TRACK_NUMBER_TAG,
                self._TITLE_TAG,
                self._ALBUM_TAG,
                self._ARTIST_TAG,
                self._ALBUM_ARTIST_TAG]

    def __str__(self):
        return self._music_file.__str__()

    @staticmethod
    def _remove_underscore(tag_name: str) -> str:
        return tag_name.replace('_', '')

    def _query_tag_value(self, tag_name: str) -> [str, None]:
        try:
            value = self._music_file[self._remove_underscore(tag_name)]
            final_value = value[0].strip() if isinstance(value, list) else value.strip()
            return final_value if final_value else None
        except (KeyError, mutagen.MutagenError):
            return None

    def _set_tag_value(self, tag_name: str, value: [str, None]) -> None:
        current_value = self._query_tag_value(tag_name)
        if value != current_value:
            final_tag_name = self._remove_underscore(tag_name)
            self._is_modified = True
            try:
                self._music_file[final_tag_name] = value if value else ''
            except mutagen.MutagenError:
                self._music_file[final_tag_name] = ''

    def _query_int_tag_value(self, tag_name: str) -> [int, None]:
        try:
            return int(self._query_tag_value(tag_name))
        except (ValueError, TypeError):
            return None

    def _set_int_tag_value(self, tag_name: str, value: [int, None]) -> None:
        try:
            self._set_tag_value(tag_name, str(value) if value else None)
        except ValueError:
            self._set_tag_value(tag_name, '')

    def reset_from_filename(self) -> None:
        self.disc_number, self.track_number, self.title = title_info_from_filename(self._file)
        self.album = self._file.parent.name
        self.artist = self._file.parent.parent.name
        self.album_artist = self.artist

    def clear_tags(self) -> None:
        for attribute in self._required_tags():
            self.__setattr__(attribute, None)

    def save_tags(self, args) -> None:
        if self._is_modified or args.force:
            info_message = f'{self._file}'
            if not args.dry_run:
                info_message += ' -- UPDATED'
                self._music_file.save()
                self._is_modified = False
            logging.info(f'{info_message} : {self.__str__()}')

    @property
    def is_book(self) -> bool:
        return '_books' in str(self._file.parent)

    @property
    def is_valid(self) -> bool:
        return all([self.__getattribute__(P) for P in self._required_tags()])

    @property
    def album(self) -> [str, None]:
        return self._query_tag_value(self._ALBUM_TAG)

    @album.setter
    def album(self, value: str) -> None:
        self._set_tag_value(self._ALBUM_TAG, value)

    @property
    def title(self) -> [str, None]:
        return self._query_tag_value(self._TITLE_TAG)

    @title.setter
    def title(self, value: [str, None]) -> None:
        self._set_tag_value(self._TITLE_TAG, value)

    @property
    def artist(self) -> [str, None]:
        return self._query_tag_value(self._ARTIST_TAG)

    @artist.setter
    def artist(self, value: [str, None]) -> None:
        self._set_tag_value(self._ARTIST_TAG, value)

    @property
    def disc_number(self) -> [int, None]:
        return self._query_int_tag_value(self._DISC_NUMBER_TAG)

    @disc_number.setter
    def disc_number(self, value: [int, None]) -> None:
        self._set_int_tag_value(self._DISC_NUMBER_TAG, value)

    @property
    def track_number(self) -> [int, None]:
        return self._query_int_tag_value(self._TRACK_NUMBER_TAG)

    @track_number.setter
    def track_number(self, value: [int, str, None]) -> None:
        self._set_int_tag_value(self._TRACK_NUMBER_TAG, value)

    @property
    def album_artist(self) -> [str, None]:
        return self._query_tag_value(self._ALBUM_ARTIST_TAG)

    @album_artist.setter
    def album_artist(self, value: [str, None]) -> None:
        self._set_tag_value(self._ALBUM_ARTIST_TAG, value)