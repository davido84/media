import re
import music_util
from pathlib import Path

_MAX_TITLE_LEN = 1000
_RE_VALID_FILENAME = re.compile(
    fr'^((?P<disc>\d{{1,2}})-)?(?P<track>\d{{1,2}}) (?P<title>[^\$]{{1,{_MAX_TITLE_LEN}}})$')  # Optional disc

_RE_FILENAME_MASKS: list[re.Pattern] = [
    re.compile(r'^(?P<disc>\d{1,2})-(?P<track>\d{1,2})_(?P<title>[^\$]+$)'),
    re.compile(r'^(?P<disc>\d{1,2})-(?P<track>\d{1,3})-(?P<title>[^\$]+$)'),
    re.compile(r'^(?P<track>\d{1,2})-(?P<title>[^\$]+$)')
]


def fix(file: Path) -> None | Path:
    for mask in _RE_FILENAME_MASKS:
        if (match := mask.match(file.stem)) is not None:
            match_values = match.groupdict()
            disc = int(match_values.get('disc', 1))
            track = int(match_values['track'])
            title = match_values['title']
            if track >= 100:
                disc = 2
                track = track-100+1
            new_stem = music_util.normalize(f'{disc:02d}-{track:02d} {title}'[:_MAX_TITLE_LEN-1])
            new_filename = file.with_stem(new_stem.replace('_', ' ').strip())

            assert(_RE_VALID_FILENAME.match(new_filename.stem))
            return new_filename

    return None


def validate(filename: Path) -> bool:
    return _RE_VALID_FILENAME.match(filename.stem) is not None