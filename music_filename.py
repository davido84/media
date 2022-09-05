import re
from pathlib import Path
import unicodedata

_MAX_TITLE_LEN = 1000
_RE_VALID_FILENAME = re.compile(
    fr'^((?P<disc>\d{{1,2}})-)?(?P<track>\d{{1,2}}) (?P<title>[^\$]{{1,{_MAX_TITLE_LEN}}})$')

_RE_FILENAME_MASKS: list[re.Pattern] = [
    re.compile(r'^(?P<disc>\d{1,2})-(?P<track>\d{1,2})_(?P<title>[^\$]+$)')
]


def fix(file: Path) -> None | Path:
    for mask in _RE_FILENAME_MASKS:
        if (match := mask.match(file.stem)) is not None:
            disc = int(match.group('disc'))
            track = int(match.group('track'))
            title = match.group('title')
            new_stem = unicodedata.normalize('NFKD', f'{disc:02d}-{track:02d} {title}'[:_MAX_TITLE_LEN-1]).strip()
            new_filename = file.with_stem(new_stem)
            assert(_RE_VALID_FILENAME.match(new_filename.stem))
            return new_filename

    return None


def validate(filename: Path) -> bool:
    return _RE_VALID_FILENAME.match(filename.stem) is not None