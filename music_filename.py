import logging
import re
import music_util
from pathlib import Path


_MAX_TITLE_LEN = 100+5  # dd-tt


_RE_CANONICAL_FILE_STEM = re.compile(
    fr'^((?P<disc>\d{{1,2}})-)?(?P<track>\d{{1,3}}) (?P<title>[^\\/$<>|?*":]{{1,{_MAX_TITLE_LEN+1}}})$')

_RE_FILENAME_MASKS: list[re.Pattern] = [
    re.compile(r'^Ep(?P<track>\d{1,3})_\d+\$(?P<title>[^$]+).*$'),
    re.compile(r'^(?P<disc>\d{1,3})-(?P<track>\d{1,3})_\d+\$(?P<title>[^$]+).*$'),
    re.compile(r'^(?P<track>\d{1,3})-(?P<disc>\d{1,3})_\d+_(?P<title>[^$])+.*$'),
    re.compile(r'^(?P<track>\d{1,2})\$(?P<title>[^$]+)\$(?P<disc>\d{1,2})\$.*$'),
    re.compile(r'^(?P<track>\d{1,2})\$(?P<title>[^$]+)\$1\$1\$.*$'),
    re.compile(r'^(?P<track>\d{1,2})_(?P<title>[^$]+)\$(?P<disc>\d{1,2})$'),
    re.compile(r'^(?P<track>\d{1,2})_(?P<title>[^$]+)\$(?P<disc>\d)\$\d$'),
    re.compile(r'^(?P<disc>\d{1,2})-(?P<track>\d{1,3})_(?P<title>[^$]+$)'),
    re.compile(r'^(?P<disc>\d{1,2})-(?P<track>\d{1,3})-(?P<title>[^$]+$)'),
    re.compile(r'^(?P<track>\d{1,3})-(?P<title>[^$]+$)')
]


def _parse_match(match_dict: dict[str, str]) -> [int, int, str]:
    return int(match_dict.get('disc', 1)), int(match_dict['track']), match_dict['title']


def fix(file: Path) -> None | Path:
    assert not validate(file)
    trimmed = file.with_stem(file.stem[:_MAX_TITLE_LEN].strip())
    if trimmed != file and validate(trimmed):
        return trimmed

    for mask in _RE_FILENAME_MASKS:
        if (match := mask.match(file.stem)) is not None:
            disc, track, title = _parse_match(match.groupdict())
            formatted_track = f'{track:02d}' if track < 100 else f'{track}'
            new_stem = music_util.normalize(f'{disc:02d}-{formatted_track} {title}'[:_MAX_TITLE_LEN - 1]).strip()
            new_stem = re.sub(r'\s+', ' ', new_stem)
            new_filename = file.with_stem(new_stem.replace('_', ' '))

            if not validate(new_filename):
                logging.error(f'Could not validate: {new_filename}')

            return new_filename

    return None


def validate(filename: Path) -> bool:
    return _RE_CANONICAL_FILE_STEM.match(filename.stem) is not None
