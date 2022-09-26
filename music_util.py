from pathlib import Path
import unicodedata


def is_book(filename: Path) -> bool:
    return '_books' in str(filename)


def normalize(s) -> str:
    new_form = unicodedata.normalize('NFKD', s)
    assert '\\' not in new_form
    return u''.join([c for c in new_form if not unicodedata.combining(c)])


def music_files(root: Path, extensions: list[str] | None = None):
    ext_list = ['flac', 'mp3'] if extensions is None else extensions
    for ext in ext_list:
        for file in root.rglob(f'*.{ext}'):
            yield Path(file)


def album_folders(root: Path) -> set[Path]:
    result: set[Path] = set()
    for file in music_files(root):
        result.add(Path(file.parent))
    return result
