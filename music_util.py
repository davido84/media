from pathlib import Path
import unicodedata


def normalize(s) -> str:
    new_form = unicodedata.normalize('NFKD', s)
    assert '\\' not in new_form
    return u''.join([c for c in new_form if not unicodedata.combining(c)])


def music_files(root: Path):
    for ext in ['flac', 'mp3', 'wav']:
        for file in root.rglob(f'*.{ext}'):
            yield Path(file)


def album_folders(root: Path) -> set[Path]:
    result: set[Path] = set()
    for file in music_files(root):
        result.add(Path(file.parent))
    return result