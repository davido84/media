from pathlib import Path


def music_files(root: Path):
    for ext in ['flac', 'mp3', 'wav']:
        for file in root.rglob(f'*.{ext}'):
            yield file


def all_music_files(root: Path) -> list[Path]:
    return [F for F in music_files(root)]