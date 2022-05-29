import click
import pyaml
from settings import VideoManager
import logging
from dataclasses import dataclass, field, asdict
from pprint import pformat
from mediautil import gigabyte_string, run_makemkvcon, gigabytes
from pathlib import Path
from discinfo import DiscInfo, parse_disc
import pickle

logger = logging.getLogger('SCAN')


@dataclass
class Result:
    skipped: int = 0
    successful: int = 0
    errors: int = 0
    corrupt: int = 0
    timeouts: int = 0
    called_process_errors: int = 0
    max_title_size: int = field(repr=False, default=0)


def command(settings: VideoManager, timeout: int, input_folder: Path) -> int:
    result = Result()
    all_files = settings.scan_for_video_files(input_folder)
    info_dict: dict[str, DiscInfo] = settings.info_dict() if not settings.force else {}

    for iso_file in all_files:
        if not settings.force and str(iso_file) in info_dict:
            result.skipped += 1
            continue

        is_dvd = iso_file.stat().st_size < gigabytes(9)
        click.secho(str(iso_file)+'...', nl=False, fg=('bright_magenta' if is_dvd else 'bright_blue'))
        mkv_result = run_makemkvcon(iso_file.name, [f'--minlength={settings.minimum_title_len}',
                                                    'info', f'iso:{iso_file!s}'], timeout)
        if mkv_result.timed_out:
            click.secho('TIMEOUT', fg='red')
            logging.warning(f'{iso_file!s: }TIMEOUT')
            result.timeouts += 1
            continue
        elif mkv_result.return_code != 0:
            click.secho('CALLED_PROCESS_ERROR', fg='red')
            logging.error(f'{iso_file!s}: CALLED_PROCESS_ERROR')
            result.called_process_errors += 1
            continue

        disc_info: DiscInfo = parse_disc(mkv_result.stdout)

        if disc_info.title_count == 0:
            click.secho('ZERO_TITLE_COUNT', fg='red')
            logging.error('Title count == o')
            result.errors += 1
            continue

        if disc_info.is_corrupt:
            click.secho('POSSIBLE_CORRUPT', fg='bright_yellow')
            logging.warning(f'{iso_file!s}: Possibly corrupt.')
            result.corrupt += 1
        else:
            click.secho('OK', fg='bright_green')

        info_dict[str(iso_file)] = disc_info

    if settings.data_folder is not None:
        yaml_dict = {}
        for key, value in info_dict.items():
            yaml_dict[key] = asdict(value)

        Path(settings.data_folder, 'discs.yaml').write_text(pyaml.dump(yaml_dict))
        settings.save_info_dict(info_dict)

    logger.info('%s', pformat(result))
    logger.info(f'Max title size: {gigabyte_string(result.max_title_size)}')
    return 0