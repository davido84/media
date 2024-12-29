from pickle import NONE
import subprocess
import shutil
import re
import logging
from pathlib import Path
from enum import Enum

class MediaType:
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
    media_type: MediaType = MediaType.HOME

    def __init__(self, iso_file: Path, root_path: Path):
        final_name: str = iso_file.stem if iso_file.parent == root_path else iso_file.parent.stem

        self.season, self.disc = tv_disc(iso_file.stem)
        self.tvdb = title_tvdb(final_name)
        self.imdb = title_imdb(final_name)
        self.year = title_year(final_name)
        self.title = title_name(final_name)

        if self.tvdb is not None:
            self.media_type = MediaType.TV
        elif self.year is not None:
            self.media_type = MediaType.MOVIE
        else:
            self.media_type = MediaType.HOME

    def __repr__(self):
        result = f'{self.year} '
        result += f'{str(self.media_type)} '
        result += f'"{self.title}"'
        
        if self.imdb is not None:
            result += f', IMDB {self.imdb}'
        if self.is_tv() and self.tvdb:
                result += f', TVDB {self.tvdb}'
        
        if self.season is not None:
            result += f', Season {self.season}'

        if self.disc is not None:
            result += f', Disc {self.disc}'

        return result
    
    def is_tv(self) -> bool:
        return self.media_type == MediaType.MUSIC

def title_name(filename: str) -> str:
    assert filename

    result = re.sub(r'\(\d\d\d\d\)', '', filename)
    result = re.sub(r'\{tvdb\-\d+\}', '', result)
    result = re.sub(r'\{imdb\-tt\d+\}', '', result)
    return result.strip()

def title_year(filename: str) -> int:
    if match := re.search(r'\((\d\d\d\d)\)', filename):
        assert int(match.group(1)) > 1933
        return int(match.group(1))
    return None

def title_tvdb(filename: str) -> int | None:
    match = re.search(r'\{tvdb\-(\d+)\}', filename)
    if match:
        assert int(match.group(1)) > 0
        return int(match.group(1))

    return None

def title_imdb(filename: str) -> int | None:
    match = re.search(r'\{imdb-tt(\d+)\}', filename)
    if match:
        assert int(match.group(1)) > 0
        return int(match.group(1))
    return None

def tv_disc(filename: str) -> tuple[int, int]:
    season = None
    disc = None
    if match := re.match(r'^\s*(\d+)-(\d+)\s*$', filename):
        season = int(match.group(1))
        disc = int(match.group(2))

    elif match := re.match(r'^\s*(\d+)\s*$', filename):
        disc = int(match.group(1))

    assert(season is None or season >= 1)
    assert(disc is None or disc >= 1)
    return season, disc

class RunMkvConResult:
    return_code: int = 0
    timed_out: bool = False
    stdout: list[str] = []

def run_mkvcon(title: str, args: list[str]) ->RunMkvConResult:
    final_args = [shutil.which('makemkvcon64.exe'), '--noscan', '-r', '--cache=2048']
    final_args.extend(args)

    proc = subprocess.Popen(final_args,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT)

    result = RunMkvConResult()

    if proc.returncode == 0:
        while True:
            try:
                line = proc.stdout.readline().decode('utf-8').strip()
            except UnicodeDecodeError:
                logging.warning('UnicodeDecodeError')
                continue

            result.stdout.append(line)
            if not line and proc.poll() is not None:
                break

    result.return_code = proc.returncode
    return RunMkvConResult()