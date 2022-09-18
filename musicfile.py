from typing import Any
import mutagen
from pathlib import Path
from mutagen.easyid3 import EasyID3


class MusicFile:
    def __init__(self, file: Path):
        self._file = file

        music_file: EasyID3 | Any = mutagen.File(
            str(file)) if file.suffix == '.flac' else EasyID3(str(file))

        self._title: str | None = music_file.get('title', None)
        self._artist: str | None = music_file.get('artist', None)
        self._album: str | None = music_file.get('album', None)
        self._disc: int | None = music_file.get('discnumber', None)
        self._track: int | None = music_file.get('tracknumber', None)

    @property
    def is_valid(self) -> bool:
        return all([self._title, self._artist, self._album, self._disc, self._track])

    @property
    def disc_number(self) -> int:
        return int(self._disc)

    @property
    def track_number(self) -> int:
        return int(self._track)