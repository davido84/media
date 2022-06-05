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
import threading

logger = logging.getLogger('VALIDATE')


@dataclass
class Result:
    skipped: int = 0
    removed: int = 0
    errors: int = 0
    timeouts: int = 0
    validator_failed_errors: int = 0
    total_mkv_bytes: int = 0
    total_titles_validated: int = 0
    total_mkv_bytes_human: str = ''


def command(vm: VideoManager, temp_folder: str) -> None:
    if not temp_folder or not Path(temp_folder).exists():
        click.secho('Temp folder does not exist', fg='bright_red')
        return

    result = Result()
    iso_dict = vm.iso_dict()

    timeout: bool = False

    def timeout_handler():
        nonlocal timeout
        timeout = True

    timer = threading.Timer(vm.timeout_in_hours*60*60, timeout_handler) if vm.timeout_in_hours is not None else None
    if timer is not None:
        timer.start()

    for iso_count, (iso_file, dict_info) in enumerate(iso_dict.items()):
        if timeout:
            click.secho('Finished with TIMEOUT', fg='bright_yellow')
            break

        if not Path(iso_file).exists():
            logger.info(f'Removed non-existent file: {iso_file!s}')
            result.removed += 1
            del iso_dict[iso_file]
            continue

        if not vm.force and iso_dict[iso_file].validated:
            result.skipped += 1
            continue  # Already processed

        if not iso_dict[iso_file].problematic:
            continue  # Assume non-problematic iso's will not have problems converting

        message = f'{iso_file!s} ({iso_count+1} of {len(iso_dict.items())})'
        click.secho(message, fg='cyan')
        for title_count, title in enumerate(dict_info.titles):
            output_file = Path(temp_folder, dict_info.titles[title][mkvcodes.output_file])

            run_makemkvcon(f'{output_file!s} ({title_count+1} of {len(dict_info.titles)})',
                           [f'--minlength={vm.minimum_title_len}',
                            'mkv', f'iso:{iso_file}', f'{title}', temp_folder], timeout=60*30, show_progress=True)
            if not output_file.exists():
                click.secho(' NO_OUTPUT_FILE', fg='red')
                logging.error(f'{iso_file!s}, {output_file!s}: NO_OUTPUT_FILE')
                result.errors += 1
                continue

            click.secho(f'Validating {output_file!s} ... ', nl=False, fg='yellow')

            try:
                subprocess.run([shutil.which('mkvalidator.exe'), '--quiet', '--quick', f'{output_file!s}'],
                               text=True, capture_output=True, check=True)
                click.secho('OK', fg='bright_green')
                result.total_mkv_bytes += output_file.stat().st_size
                iso_dict[iso_file].valid_titles.add(title_count)
                result.total_titles_validated += 1

            except subprocess.CalledProcessError:
                click.secho(f'FAILED', fg='bright_red')
                logging.error(f'{iso_file!s}, {output_file!s}: FAILED validation.')
                result.validator_failed_errors += 1

            finally:
                output_file.unlink(missing_ok=True)

        iso_dict[iso_file].validated = True
        vm.save_yaml_dict(iso_dict)
        vm.save_iso_dict(iso_dict)

    result.total_mkv_bytes_human = gigabyte_string(result.total_mkv_bytes)
    logger.info('%s', pformat(result))
    if timer is not None and timer.is_alive():
        timer.cancel()
