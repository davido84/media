from settings import VideoManager
from dataclasses import dataclass
import logging
from pprint import pformat
import click
import mkvcodes
from mediautil import run_makemkvcon
from pathlib import Path
import subprocess
import shutil

logger = logging.getLogger('VALIDATE')


@dataclass
class Result:
    skipped: int = 0
    successful: int = 0
    errors: int = 0
    timeouts: int = 0
    called_process_errors: int = 0


def command(vm: VideoManager, temp_folder: str):
    result = Result()
    info_dict = vm.info_dict()

    for iso_file, dict_info in info_dict.items():
        click.secho(str(iso_file), fg='cyan')
        for title in dict_info.titles:
            output_file = Path(temp_folder, dict_info.titles[title][mkvcodes.output_file])
            # click.secho(f'  {output_file}')
            run_makemkvcon(f'{output_file!s}',
                           ['mkv', f'iso:{iso_file}', f'{title}', temp_folder], timeout=60*30, show_progress=True)
            if not output_file.exists():
                click.secho('NO_OUTPUT_FILE')
                logging.error(f'{iso_file!s}, {output_file!s}: NO_OUTPUT_FILE')
                result.errors += 1
                continue
            click.secho(f'Validating {output_file!s} ... ', nl=False, fg='yellow')
            try:
                subprocess.run([shutil.which('mkvalidator.exe'), '--quiet', '--quick', f'{output_file!s}'],
                               text=True, capture_output=True, check=True)
                click.secho('OK', fg='bright_green')
            except subprocess.CalledProcessError:
                click.secho(f'FAILED', fg='bright_red')
                logging.error(f'{iso_file!s}, {output_file!s}: FAILED validation.')

            finally:
                output_file.unlink(missing_ok=True)

    logger.info('%s', pformat(result))