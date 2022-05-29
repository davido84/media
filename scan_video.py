import click
import pyaml
import collections
from settings import VideoManager
import logging
from dataclasses import dataclass, field, asdict
from pprint import pformat
from mediautil import gigabyte_string, run_makemkvcon, scan_for_video_files, gigabytes
from pathlib import Path
from discinfo import DiscInfo, parse_disc
import yaml
import pickle

logger = logging.getLogger('SCAN')


@dataclass
class Result:
    num_successful: int = 0
    num_errors: int = 0
    num_corrupt: int = 0
    num_timeouts: int = 0
    num_called_process_errors: int = 0
    max_title_size: int = field(repr=False, default=0)


def command(settings: VideoManager, timeout: int, input_folder: Path) -> int:
    # logging.info(f'Timeout={timeout}')
    # run_result = run_makemkvcon('The message', ['mkv', f'iso:e:/movies/test-g.iso', '0', 'e:/movies'])
    result = Result()
    all_files = scan_for_video_files(settings, input_folder)
    info_dict: dict[str, DiscInfo] = {}
    for iso_file in all_files:
        is_dvd = iso_file.stat().st_size < gigabytes(9)
        click.secho(str(iso_file)+'...', nl=False, fg=('bright_magenta' if is_dvd else 'bright_blue'))
        mkv_result = run_makemkvcon([f'--minlength={settings.minimum_title_len}',
                                     'info', f'iso:{iso_file!s}'], timeout)
        if mkv_result.timed_out:
            click.secho('TIMEOUT', fg='red')
            logging.warning(f'{iso_file!s: }TIMEOUT')
            result.num_timeouts += 1
            continue
        elif mkv_result.return_code != 0:
            click.secho('CALLED_PROCESS_ERROR', fg='red')
            logging.error(f'{iso_file!s}: CALLED_PROCESS_ERROR')
            result.num_called_process_errors += 1
            continue

        disc_info: DiscInfo = parse_disc(mkv_result.stdout)

        if disc_info.title_count == 0:
            click.secho('ZERO_TITLE_COUNT', fg='red')
            logging.error('Title count == o')
            result.num_errors += 1
            continue

        if disc_info.is_corrupt:
            click.secho('POSSIBLE_CORRUPT', fg='bright_yellow')
            logging.warning(f'{iso_file!s}: Possibly corrupt.')
            result.num_corrupt += 1
        else:
            click.secho('OK', fg='bright_green')

        info_dict[str(iso_file)] = disc_info

    if settings.data_folder is not None:
        yaml_dict = {}
        for key, value in info_dict.items():
            yaml_dict[key] = asdict(value)

        Path(settings.data_folder, 'discs.yaml').write_text(pyaml.dump(yaml_dict))
        with Path(settings.data_folder, 'discs.pkl').open('wb') as pickle_file:
            pickle.dump(info_dict, pickle_file)

    logger.info('%s', pformat(result))
    logger.info(f'Max title size: {gigabyte_string(result.max_title_size)}')
    return 0