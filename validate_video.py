from settings import VideoManager
from dataclasses import dataclass
import logging
from pprint import pformat
import click
import mkvcodes
from mediautil import run_makemkvcon, gigabyte_string
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
    validator_failed_errors: int = 0
    total_mkv_bytes: int = 0
    total_mkv_bytes_human: str = ''


def command(vm: VideoManager, temp_folder: str):
    result = Result()
    iso_dict = vm.iso_dict()

    for iso_count, (iso_file, dict_info) in enumerate(iso_dict.items()):
        iso_dict[iso_file].validated = True
        message = f'{iso_file!s} ({iso_count+1} of {len(iso_dict.items())})'
        click.secho(message, fg='cyan')
        for title_count, title in enumerate(dict_info.titles):
            output_file = Path(temp_folder, dict_info.titles[title][mkvcodes.output_file])
            # click.secho(f'  {output_file}')
            run_makemkvcon(f'{output_file!s} ({title_count+1} of {len(dict_info.titles)})',
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
                result.successful += 1
                result.total_mkv_bytes += output_file.stat().st_size
                iso_dict[iso_file].valid_titles.add(title_count)
            except subprocess.CalledProcessError:
                click.secho(f'FAILED', fg='bright_red')
                logging.error(f'{iso_file!s}, {output_file!s}: FAILED validation.')
                result.validator_failed_errors += 1

            finally:
                output_file.unlink(missing_ok=True)

    vm.save_yaml_dict(iso_dict)
    vm.save_iso_dict(iso_dict)

    result.total_mkv_bytes_human = gigabyte_string(result.total_mkv_bytes)
    logger.info('%s', pformat(result))