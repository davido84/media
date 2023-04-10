import subprocess
from pathlib import Path
from enum import Enum

class MkvParser:
    def _parse(self, output_lines: list[str]):
        pass

    def __init__(self, mkv_output: str):
        self._parse(mkv_output.split('\n'))

class MkvCon:
    def __init__(self,
                 input_file: str,
                 output_file: str=None,
                 min_length=180,
                 cache_size=2000,
                 ):
        pass

    # @staticmethod
    # def run(self, input_file: Path, options: list[str], output_file: Path=None) ->list[str]:
    #
    #     return [L.strip() for L in subprocess.run(
    #         ['makemkvcon64.exe', '-r', 'info', str(input_file)],
    #         check=True, capture_output=True).stdout.decode('utf-8').split('\n')]
    #     pass

class MkvScanner:
    def __init__(self, iso_file: Path):
        self._iso = iso_file

    def scan(self):
        pass