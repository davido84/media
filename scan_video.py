import click
from settings import VideoManager
import logging
from dataclasses import dataclass
from pprint import pformat
from mediautil import gigabyte_string, run_makemkvcon, gigabytes
from pathlib import Path
from isodisc import IsoDisc, parse_disc

logger = logging.getLogger('SCAN')


@dataclass
class Result:
    skipped: int = 0
    new: int = 0
    errors: int = 0
    corrupt: int = 0
    timeouts: int = 0
    called_process_errors: int = 0


def command(settings: VideoManager, timeout: int, input_folder: Path) -> int:
    result = Result()
    all_files = settings.scan_for_video_files(input_folder)
    iso_dict: dict[str, IsoDisc] = settings.iso_dict() if not settings.force else {}
    max_title_size: int = 0

    for count, iso_file in enumerate(all_files):
        if not settings.force and str(iso_file) in iso_dict:
            result.skipped += 1
            continue

        is_dvd = iso_file.stat().st_size < gigabytes(9)
        message = f'{iso_file!s} ({gigabyte_string(iso_file.stat().st_size)}) {count+1} of {len(all_files)} ...'
        click.secho(message, nl=False, fg=('bright_magenta' if is_dvd else 'bright_blue'))
        mkv_result = run_makemkvcon(message, [f'--minlength={settings.minimum_title_len}',
                                              'info', f'iso:{iso_file!s}'], timeout=timeout, show_progress=False)
        if mkv_result.timed_out:
            click.secho('TIMEOUT', fg='red')
            logging.warning(f'{iso_file!s: } TIMEOUT')
            result.timeouts += 1
            continue
        elif mkv_result.return_code != 0:
            click.secho('CALLED_PROCESS_ERROR', fg='red')
            logging.error(f'{iso_file!s}: CALLED_PROCESS_ERROR')
            result.called_process_errors += 1
            continue

        disc_info: IsoDisc = parse_disc(mkv_result.stdout)

        if disc_info.title_count == 0:
            click.secho('ZERO_TITLE_COUNT', fg='red')
            logging.error(f'{iso_file!s}: Title count == o')
            result.errors += 1
            continue

        if disc_info.possibly_corrupt:
            click.secho(' POSSIBLE_CORRUPT', fg='bright_yellow')
            logging.warning(f'{iso_file!s}: Possibly corrupt.')
            result.corrupt += 1
        else:
            click.secho(' OK', fg='bright_green')
            max_title_size = max(max_title_size, disc_info.max_title_size)

        iso_dict[str(iso_file)] = disc_info
        result.new += 1

    settings.save_yaml_dict(iso_dict)
    settings.save_iso_dict(iso_dict)

    logger.info('%s', pformat(result))
    logger.info(f'Max title size: {gigabyte_string(max_title_size)}')
    return 0
