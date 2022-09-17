import mutagen
from pathlib import Path


class MusicFile:
    def __init__(self, file_name: Path):
        music_file = mutagen.File(str(file_name))
        self._title: str | None = music_file.get('title', None)
        self._artist: str | None = music_file.get('artist', None)
        self._album: str | None = music_file.get('album', None)
        self._disc: int | None = int(music_file.get('discnumber', None))
        self._track: int | None = int(music_file.get('tracknumber', None))

    @property
    def is_valid(self) -> bool:
        return all([self._title, self._artist, self._album, self._disc is not None, self._track is not None])

    @property
    def disc_number(self) -> int | None:
        return int(self._disc) if self._disc is not None else None

    @property
    def track_number(self) -> int | None:
        return int(self._track) if self._track is not None else None