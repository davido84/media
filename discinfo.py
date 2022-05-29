from dataclasses import dataclass, field
import mkvcodes
import re
from mediautil import gigabyte_string


@dataclass
class DiscInfo:
    title_count: int = 0
    max_title_size: int = 0
    max_title_size_human: str = None
    is_corrupt: bool = False
    disc_info: dict[int, str | int] = field(default_factory=dict[int, str | int])
    titles: dict[int, dict[int, str | int]] = field(
        default_factory=dict[int, dict[int, str | int]])
    streams: dict[int, dict[int, dict[int, str | int]]] = field(
        default_factory=dict[int, dict[int, dict[int, str | int]]])


def _parse_value(value: str) -> str | int:
    return int(value) if re.match(r'\d+$', value) else value


def _filter_code(code: int) -> bool:
    return code not in (mkvcodes.panel_title,
                        mkvcodes.segments_map,
                        mkvcodes.tree_info)


def parse_disc(mkv_info: list[str]) -> DiscInfo:
    disc_info = DiscInfo()

    value_re = r'"([^"]+)"'

    for line in mkv_info:
        if match := re.match(r'TCOUNT:(\d+)', line):
            disc_info.title_count = int(match.group(1))
        elif line.startswith('MSG:4004'):
            disc_info.is_corrupt = True
        elif match := re.match(fr'CINFO:(\d+),(\d+),{value_re}', line):
            code = int(match.group(1))
            if _filter_code(code):
                if code == mkvcodes.info:
                    disc_info.disc_info[int(match.group(2))] = match.group(3)
                else:
                    disc_info.disc_info[code] = _parse_value(match.group(3))
        elif match := re.match(fr'TINFO:(\d+),(\d+),(\d+),{value_re}', line):
            title_num = int(match.group(1))
            code = int(match.group(2))
            value = _parse_value(match.group(4))
            if _filter_code(code):
                if title_num not in disc_info.titles:
                    disc_info.titles[title_num] = {}
                disc_info.titles[title_num][code] = value
                if code == mkvcodes.title_size:
                    disc_info.max_title_size = max(disc_info.max_title_size, int(value))
                    disc_info.max_title_size_human = gigabyte_string(disc_info.max_title_size)

        elif match := re.match(fr'SINFO:(\d+),(\d+),(\d+),(\d+),{value_re}', line):
            title_num = int(match.group(1))
            substream_num = int(match.group(2))
            code = int(match.group(3))
            if _filter_code(code):
                info_code = int(match.group(4))
                value = _parse_value(match.group(5))
                if title_num not in disc_info.streams:
                    disc_info.streams[title_num] = {}
                if substream_num not in disc_info.streams[title_num]:
                    disc_info.streams[title_num][substream_num] = {}
                if code == mkvcodes.info:
                    code = info_code
                disc_info.streams[title_num][substream_num][code] = value

    return disc_info