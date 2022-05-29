from dataclasses import dataclass, field
import mkvcodes
import re
from collections import defaultdict


@dataclass
class DiscInfo:
    title_count: int = 0
    is_corrupt: bool = False
    disc_info: dict[int, str] = field(default_factory=dict[int, str])
    titles: dict[int, dict[int, str]] = field(default_factory=dict[int, dict[int, str]])
    streams: dict[int, dict[int, dict[int, str]]] = field(default_factory=dict[int, dict[int, dict[int, str]]])


def parse_disc(mkv_info: list[str]) -> DiscInfo:
    disc_info = DiscInfo()

    for line in mkv_info:
        if match := re.match(r'TCOUNT:(\d+)', line):
            disc_info.title_count = int(match.group(1))
        elif line.startswith('MSG:4004'):
            disc_info.is_corrupt = True
        elif match := re.match(r'CINFO:(\d+),(\d+),("[^"]+")', line):
            code = int(match.group(1))
            if code == mkvcodes.info:
                disc_info.disc_info[int(match.group(2))] = match.group(3)
            else:
                disc_info.disc_info[code] = match.group(3)
        elif match := re.match(r'TINFO:(\d+),(\d+),(\d+),("[^"]+")', line):
            title_num = int(match.group(1))
            code = int(match.group(2))
            value = match.group(4)
            if title_num not in disc_info.titles:
                disc_info.titles[title_num] = {}
            title_dict = disc_info.titles[title_num][code] = value
        elif match := re.match(r'SINFO:(\d+),(\d+),(\d+),(\d+),("[^"]+")', line):
            title_num = int(match.group(1))
            substream_num = int(match.group(2))
            code = int(match.group(3))
            info_code = int(match.group(4))
            value = match.group(5)
            if title_num not in disc_info.streams:
                disc_info.streams[title_num] = {}
            if substream_num not in disc_info.streams[title_num]:
                disc_info.streams[title_num][substream_num] = {}
            if code == mkvcodes.info:
                code = info_code
            disc_info.streams[title_num][substream_num][code] = value

    return disc_info