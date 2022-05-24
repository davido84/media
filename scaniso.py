import argparse
from dataclasses import dataclass, field
from pathlib import Path
import logging
import sys
import globalvideosettings
from mediautil import gigabyte_string, Timer, log_list, gigabytes
from colorama import Fore
import colorama
from cv import DiscInfo
from pprint import pformat
import subprocess
from datetime import datetime
import shutil
from globalvideosettings import GlobalVideoSettings


@dataclass
class ProgramArgs:
    input: Path = None
    timeout: int = 3*60  # default timeout is 3 minutes
    force: bool = False


@dataclass
class Output:
    max_file_size: int = 0
    elapsed_time: str = ''
    skipped_files: list[str] = field(default_factory=list[str])
    successful_files: list[str] = field(default_factory=list[str])
    failed_files: list[str] = field(default_factory=list[str])
    timeout_files: list[str] = field(default_factory=list[str])
    corrupt_files: list[str] = field(default_factory=list[str])


def _logger_name() -> str:
    return 'scan_iso'


def scan_iso(settings: ProgramArgs) -> Output:
    assert settings.input is not None

    global_settings = GlobalVideoSettings()

    output = Output()
    colorama.init(autoreset=True)

    # logging.basicConfig(filename=Path(global_settings.media_config_folder, 'check-iso.log'),
    #                     encoding='utf-8', level=logging.INFO, filemode=('w' if settings.force else 'a'),
    #                     format='%(asctime)s %(levelname)s: %(message)s', datefmt='%m/%d %I:%M %p')

    # logger = logging.getLogger(_logger_name())
    logger = global_settings.logger

    logger.info('Started')
    logger.info('%s', pformat(settings))

    # discs_yaml = Path(settings.output, 'discs.yaml')
    # discs_pickle = Path(settings.output, 'discs.pkl')
    # disc_info_dict: dict[Path, DiscInfo] = {}

    with global_settings.disc_info_dict(settings.force) as disc_info_dict:
        # if settings.force:
        #     discs_yaml.unlink(missing_ok=True)
        #     discs_pickle.unlink(missing_ok=True)

        all_files = [F for F in settings.input.rglob('*.iso')]

        with Timer() as t:
            # if discs_pickle.exists():
            #     disc_info_dict = pickle.load(discs_pickle.open('rb'))

            for count, iso_file in enumerate(all_files):
                if str(iso_file) in disc_info_dict:
                    output.skipped_files.append(f'{iso_file!s}')
                    continue

                file_size = iso_file.stat().st_size

                # Assumed that BD is > 9GB
                is_blu_ray = file_size > gigabytes(9)

                sys.stdout.write(f'{iso_file.parent}')
                sys.stdout.write(fr'\{Fore.BLUE if is_blu_ray else Fore.LIGHTMAGENTA_EX}{iso_file.stem}.iso ')
                sys.stdout.write(f'{count + 1} of {len(all_files)}: ')
                now = datetime.now().time()
                sys.stdout.write(f'[{gigabyte_string(file_size)}] {now.strftime("%I:%M %p")} ... ')

                try:
                    mkv_output = subprocess.run([shutil.which('makemkvcon64.exe'), '-r', '--noscan',
                                                 '--minlength', f'{global_settings.mkv_minlength}', 'info',
                                                 f'iso:{str(iso_file)}'], text=True, capture_output=True,
                                                timeout=settings.timeout, check=True)

                    disc_info = DiscInfo(mkv_output.stdout)
                    disc_info._source_file = f'"{iso_file!s}"'

                    validation_code = disc_info.validate()
                    if validation_code == DiscInfo.ValidationCode.OK or DiscInfo.ValidationCode.POSSIBLE_ERROR:
                        output.max_file_size = max(disc_info.max_video_stream_size(), output.max_file_size)
                        disc_info_dict[iso_file] = disc_info
                        output.successful_files.append(str(iso_file))

                except subprocess.CalledProcessError:
                    output.failed_files.append(str(iso_file))
                    validation_code = DiscInfo.ValidationCode.PROCESS_ERROR

                except subprocess.TimeoutExpired:
                    output.timeout_files.append(str(iso_file))
                    validation_code = DiscInfo.ValidationCode.TIMEOUT

                fore_color = Fore.LIGHTGREEN_EX
                status_log_level = logging.INFO
                if validation_code == DiscInfo.ValidationCode.POSSIBLE_ERROR:
                    status_log_level = logging.WARNING
                    fore_color = Fore.LIGHTYELLOW_EX
                    output.corrupt_files.append(str(iso_file))
                elif validation_code != DiscInfo.ValidationCode.OK:
                    status_log_level = logging.ERROR
                    fore_color = Fore.RED
                    output.failed_files.append(str(iso_file))

                sys.stdout.write(f'{fore_color}{validation_code.name}\n')

                if validation_code != DiscInfo.ValidationCode.OK:
                    log_message: str = f'{iso_file!s}, {gigabyte_string(file_size)}, Validation: {validation_code.name}'
                    logger.log(status_log_level, log_message)

    output.elapsed_time = t.format_delta()

    # discs_yaml.write_text(yaml.dump(disc_info_dict))

    logger.info('Finished scanning files.')

    logger.info(f'{len(output.successful_files)} succeeded.')
    logger.info(f'{len(output.skipped_files)} skipped.')
    log_list('Failed files:', output.failed_files, global_settings.logger, logging.ERROR)
    log_list('Timeout files:', output.timeout_files, global_settings.logger, logging.ERROR)
    log_list('Corrupt files:', output.corrupt_files, global_settings.logger, logging.WARNING)
    logger.info(f'Maximum output file size: {gigabyte_string(output.max_file_size)}')
    logger.info(f'Elapsed running time: {output.elapsed_time}')
    logger.info('Finished.')
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description='Scan ISO images with makemkv info')
    parser.add_argument('-i', '--input', help='Input folder', type=Path)
    parser.add_argument('-f', '--force', help='Force scan even if already scanned', type=bool,
                        action=argparse.BooleanOptionalAction)
    program_args = ProgramArgs()

    parser.parse_args(namespace=program_args)
    if program_args.input is None:
        parser.print_help()
    else:
        scan_iso(program_args)
    return 0


if __name__ == "__main__":
    sys.exit(main())