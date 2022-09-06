from pathlib import Path


def music_files(root: Path):
    for ext in ['flac', 'mp3', 'wav']:
        for file in root.rglob(f'*.{ext}'):
            yield Path(file)


def album_folders(root: Path) -> set[Path]:
    result: set[Path] = set()
    for file in music_files(root):
        result.add(Path(file.parent))
    return result


# def all_music_files(root: Path) -> list[Path]:
#     return [F for F in music_files(root)]