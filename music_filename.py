import re

_RE_VALID_FILENAME = re.compile(r'^((?P<disc>[1-9]{1,2})-)?(?P<track>\d{1,3}) (?P<title>[^\$]+)$')


def validate(filename: str) -> bool:
    return _RE_VALID_FILENAME.match(filename) is not None