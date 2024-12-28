import os
from tarfile import is_tarfile
import threading
import subprocess
import shutil
import re
import logging
from pathlib import Path

def rename(file: Path, new_name: Path, dry_run: bool) ->None:
    if str(file) != str(new_name):
        if dry_run:
            print(f'[RENAME] {str(file)} -> {str(new_name)}')
        else:
            file.rename(new_name)

class IsoTitleInfo:
    title: str
    year: int | None
    tvdb: int | None
    imdb: int | None
    season: int | None
    disc: int | None
    is_tv: bool

    def __repr__(self):
        result = f'"{self.title}"'
        if self.is_tv:
            result += ' (TV)'
        if self.year is not None:
            result += f',{self.year}'
        if self.imdb is not None:
            result += f',IMDB {self.imdb}'
        if self.is_tv:
            if self.tvdb:
                result += f',{self.tvdb}'
            result += f',Season {self.season},Disc {self.disc}'
        return result

    def __init__(self, iso_file: Path):
        self.season, self.disc = tv_disc(iso_file.stem)
        self.is_tv = self.season is not None

        file_name: str = iso_file.parent.stem if self.is_tv else iso_file.stem

        self.imdb = title_imdb(file_name)
        self.tvdb = title_tvdb(file_name)
        self.year = title_year(file_name)
        self.title = title_name(file_name)


def title_name(filename: str) -> str:
    assert title_name
    if match := re.match(r'^(.*)\(\d\d\d\d\).*$', filename):
        return match.group(1).strip()
    elif match := re.search(r'^(.*)\{tvdb.*$', filename):
        return match.group(1).strip()
    return filename.strip()

def title_year(filename: str) -> int:
    if match := re.search(r'\((\d\d\d\d)\)', filename):
        assert int(match.group(1)) > 1920
        return int(match.group(1))
    return None

def title_tvdb(filename: str) -> int | None:
    match = re.search(r'\{tvdb\-(\d+)\}$', filename)
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
    if match := re.match(r'^\s*(\d+)-(\d+).*$', filename):
        season = int(match.group(1))
        disc = int(match.group(2))

    elif match := re.match(r'^\s*\d+$', filename):
        season = 1
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