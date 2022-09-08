import re
import music_util
from pathlib import Path


_MAX_TITLE_LEN = 1000
_RE_CANONICAL_FILE_STEM = re.compile(
    fr'^((?P<disc>\d{{1,2}})-)?(?P<track>\d{{1,2}}) (?P<title>[^\\/$<>|?*":]{{1,{_MAX_TITLE_LEN}}})$')

_RE_FILENAME_MASKS: list[re.Pattern] = [
    re.compile(r'^(?P<disc>\d{1,2})-(?P<track>\d{1,2})_(?P<title>[^\$]+$)'),
    re.compile(r'^(?P<disc>\d{1,2})-(?P<track>\d{1,3})-(?P<title>[^\$]+$)'),
    re.compile(r'^(?P<track>\d{1,2})-(?P<title>[^\$]+$)')
]


def _parse_match(match_dict: dict[str, str]) -> [int, int, str]:
    return int(match_dict.get('disc', 1)), int(match_dict['track']), match_dict['title']


def fix(file: Path) -> None | Path:
    assert not validate(file)

    for mask in _RE_FILENAME_MASKS:
        if (match := mask.match(file.stem)) is not None:
            disc, track, title = _parse_match(match.groupdict())
            if track >= 100:
                disc = 2
                track = track - 100 + 1
            new_stem = music_util.normalize(f'{disc:02d}-{track:02d} {title}'[:_MAX_TITLE_LEN - 1])
            new_filename = file.with_stem(new_stem.replace('_', ' ').strip())

            assert validate(new_filename)
            return new_filename

    return None


def validate(filename: Path) -> bool:
    return _RE_CANONICAL_FILE_STEM.match(filename.stem) is not None