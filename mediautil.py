import re
from pathlib import Path
from enum import Enum
import subprocess
from subprocess import CompletedProcess

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

def run_external(name: str, args: list[str], capture_output: bool) -> CompletedProcess:
    stdout_capture = subprocess.PIPE if capture_output else subprocess.DEVNULL
    stderr_capture = subprocess.PIPE if capture_output else subprocess.DEVNULL
    final_args = [name]
    final_args.extend(args)
    return subprocess.run(final_args,
        stdout=stdout_capture, stderr=stderr_capture, text=True)

def run_handbrake(file: Path, args: list[str], capture_output: bool=True) -> CompletedProcess:
    final_args = ['-i', str(file)]
    final_args.extend(args)
    return run_external('handbrakecli.exe', final_args, capture_output)
