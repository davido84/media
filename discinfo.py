from dataclasses import dataclass, field
from enum import Enum, auto
import re


@dataclass
class DiscInfo:
    title_count: int = None
    is_corrupt: bool = None
    messages: dict[int, str] = field(default_factory=dict[int, str])
    disc_info: dict[int, str] = field(default_factory=dict[int, str])
    title_info: list[dict[int, str]] = field(default_factory=list[dict[int, str]])
    stream_info: list[dict[int, str]] = field(default_factory=list[dict[str]])


def parse_disc(mkv_info: list[str]) -> DiscInfo:
    disc_info = DiscInfo()
    for line in mkv_info:
        if match := re.match(r'TCOUNT:(\d+)', line):
            disc_info.title_count = int(match.group(1))

    return disc_info