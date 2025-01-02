import re
import logging
from pathlib import Path
from enum import Enum
import subprocess
import json
from subprocess import CompletedProcess
from datetime import timedelta

def get_logger():
    return logging.getLogger('ConvertMedia')

_DRY_RUN_PREFIX = '[DRY RUN] : '

_RE_TVDB = re.compile(r'\{tvdb\s*-\s*(\d+)}', re.IGNORECASE)
_RE_IMDB = re.compile(r'\{imdb\s*-\s*tt(\d+)}', re.IGNORECASE)
_RE_YEAR = re.compile(r'\(\s*(\d\d\d\d)\s*\)')
_RE_SEASON_DISC = re.compile(r'^\s*(\d+)\s*-\s*(\d+)\s*$')
_RE_DISC = re.compile(r'^\s*(\d+)\s*$')

class MediaType(Enum):
    TV = 'TV'
    MOVIE = 'MOVIE'
    MUSIC = 'MUSIC'
    HOME = 'HOME'

class DiscTitle:
    number: int
    picture_width: int
    duration: timedelta

    def __init__(self, title_number: int):
        self.number = title_number

    def __repr__(self):
        return f'{self.number}, Width: {self.picture_width}, Duration: {self.duration}, Total Seconds: {int(self.duration.total_seconds())}'

def scan_media_file(filename: Path) -> tuple[int, list[DiscTitle]]:
    titles: list[DiscTitle] = []
    main_feature: int = 1
    json_dict: dict = {}

    with open(str(filename.with_suffix('.json')), 'r') as file:
        json_dict = json.load(file)  # Parse JSON into a dictionary

    main_feature = int(json_dict['MainFeature']) if 'MainFeature' in json_dict else 1
    for title_number, title_dict in enumerate(json_dict['TitleList'], start=0):
        title = DiscTitle(title_number+1)
        title.picture_width = int(title_dict['Geometry']['Width'])
        title.duration = timedelta(hours=int(title_dict['Duration']['Hours']),
                                   minutes=int(title_dict['Duration']['Minutes']),
                                   seconds=int(title_dict['Duration']['Seconds']))

        titles.append(title)

    return main_feature, titles

class IsoTitleInfo:
    title: str
    year: int | None
    tvdb: int | None
    imdb: int | None
    season: int | None
    disc: int | None
    media_type: MediaType

    def __init__(self, iso_file: Path, root_path: Path):
        final_name: str = iso_file.stem if iso_file.parent == root_path else iso_file.parent.stem

        self.season, self.disc = tv_disc(iso_file.stem)
        self.tvdb = title_tvdb(final_name)
        self.imdb = title_imdb(final_name)
        self.year = title_year(final_name)

        if self.tvdb is not None:
            self.media_type = MediaType.TV
        elif self.year is not None:
            self.media_type = MediaType.MOVIE
        else:
            self.media_type = MediaType.HOME

        self.title = title_name(final_name)

        if self.is_tv() and self.season is None:
            self.season = 1

    def __repr__(self):
        result = f'{self.year} '
        result += f'{str(self.media_type)} '
        result += f'"{self.title}"'
        
        if self.imdb is not None:
            result += f',IMDB {self.imdb}'
        if self.is_tv() and self.tvdb:
                result += f',TVDB {self.tvdb}'
        
        if self.season is not None:
            result += f', Season {self.season}'

        if self.disc is not None:
            result += f',Disc {self.disc}'

        return result
    
    def is_tv(self) -> bool:
        return self.media_type == MediaType.TV

def title_name(filename: str) -> str:
    result = _RE_YEAR.sub('', filename)
    result = _RE_TVDB.sub('', result)
    result = _RE_IMDB.sub('', result)

    return result.strip()

def title_year(filename: str) -> int | None:
    if match := _RE_YEAR.search(filename):
        return int(match.group(1))
    return None

def title_tvdb(filename: str) -> int | None:
    match = _RE_TVDB.search(filename)
    if match:
        return int(match.group(1))

    return None

def title_imdb(filename: str) -> int | None:
    match = _RE_IMDB.search(filename)
    if match:
        return int(match.group(1))
    return None

def tv_disc(filename: str) -> tuple[int, int]:
    season = None
    disc = None
    if match := _RE_SEASON_DISC.match(filename):
        season = int(match.group(1))
        disc = int(match.group(2))

    elif match := _RE_DISC.match(filename): 
        disc = int(match.group(1))

    return season, disc

def rename_file(file: Path, new_name: Path, dry_run: bool) -> Path:
    if str(file) != str(new_name):
        info = f'"{str(file)}" -> "{new_name}"'
        if dry_run:
            get_logger().info(f'{_DRY_RUN_PREFIX} (rename) {info}')
        else:
            try:
                get_logger().info(f'* Renamed file {info}')
                return file.rename(new_name)
            except OSError:
                get_logger().critical(f'Error renaming: {str(file)}')

    return new_name

def run_external(name: str, args: list[str], capture_output: bool, dry_run) -> CompletedProcess:
    stdout_capture = subprocess.PIPE if capture_output else subprocess.DEVNULL
    stderr_capture = subprocess.PIPE if capture_output else subprocess.DEVNULL
    final_args = [name]
    final_args.extend(args)
    if dry_run:
        get_logger().info(_DRY_RUN_PREFIX + ','.join(final_args))
        return CompletedProcess(args=[], returncode=0)

    return subprocess.run(final_args,
        stdout=stdout_capture, stderr=stderr_capture, text=True, check=True)

def run_handbrake(file: Path, args: list[str], capture_output: bool=True,
                  dry_run: bool=False) -> CompletedProcess:
    final_args = ['-i', str(file)]
    final_args.extend(args)
    return run_external('handbrakecli.exe', final_args, capture_output, dry_run)
