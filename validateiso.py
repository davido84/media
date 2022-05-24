import enum
import subprocess
import argparse
import sys
from pathlib import Path
from dataclasses import dataclass, field
import logging
import pickle
import colorama
from colorama import Fore
from cv import DiscInfo
import shutil
from mediautil import gigabyte_string, Timer
from enum import Enum
from globalvideosettings import GlobalVideoSettings


@dataclass
class ProgramArgs:
    output: Path = None
    force: bool = False
    data_dict_path: Path = None
    temp_path: Path = None
    process_corrupt: bool = True
    process_success: bool = True


@dataclass
class ValidateIsoOutput:
    total_bytes: int = 0
    elapsed_time: str = None
    failed_iso: list[str] = field(default_factory=list[str])


def validate_iso(program_args: ProgramArgs) -> ValidateIsoOutput:
    class ValidateResult(Enum):
        OK = enum.auto()
        MAKEMKV_ERROR = enum.auto()
        NO_OUTPUT = enum.auto()
        FAILED = enum.auto()

    output = ValidateIsoOutput()
    global_settings = GlobalVideoSettings()

    colorama.init(autoreset=True)
    logging.basicConfig(filename=Path(program_args.output, 'validate-iso.log'),
                        encoding='utf-8', level=logging.INFO, filemode=('w' if program_args.force else 'a'),
                        format='%(asctime)s %(levelname)s: %(message)s', datefmt='%m/%d %I:%M %p')

    data_dict: dict[Path, DiscInfo] = pickle.load(program_args.data_dict_path.open('rb'))
    logger = logging.getLogger(_logger_name())
    logger.info('Started.')
    with Timer(verbose=True) as timer:
        for iso_file, disc_info in data_dict.items():
            if any([not program_args.process_corrupt and disc_info.status_corrupt,
                    not program_args.process_success and not disc_info.status_success,
                    not iso_file.exists()]):
                continue

            test_titles = [T for T in disc_info.titles if T.has_content()]
            if not test_titles:
                continue
            logger.info(f'{iso_file!s}')
            assert disc_info.status_success or disc_info.status_corrupt
            print_color = f'{Fore.LIGHTYELLOW_EX}' if disc_info.status_corrupt else f'{Fore.GREEN}'
            sys.stdout.write(f'Testing file: {print_color}{iso_file.stem}\n')
            validate_result = ValidateResult.OK

            for title in test_titles:
                logger.info(f'   Exporting {title.file_output}')
                sys.stdout.write(f'  {Fore.LIGHTBLUE_EX}{title.file_output}')
                sys.stdout.write(f' {Fore.RESET}[{gigabyte_string(title.size)}]')
                sys.stdout.write(f' {title.length} {Fore.LIGHTCYAN_EX}[exporting] ')
                try:
                    subprocess.run([shutil.which('makemkvcon64.exe'), '--noscan', f'--minlength',
                                    f'{global_settings.mkv_minlength!s}', '--cache', '1024',
                                    'mkv', f'iso:{str(iso_file)}', f'{title.canonical_id}',
                                    f'{program_args.temp_path!s}'], text=True, capture_output=True, check=True)
                except subprocess.CalledProcessError:
                    validate_result = ValidateResult.MAKEMKV_ERROR
                    sys.stdout.write(f'{Fore.RED} CalledProcessError!')

                test_file_path = Path(program_args.temp_path, title.file_output)
                if validate_result != ValidateResult.MAKEMKV_ERROR and test_file_path.exists():
                    try:
                        logger.info(f'   Validating {title.file_output!s}')
                        sys.stdout.write(f'{Fore.LIGHTCYAN_EX}[validating] ')
                        subprocess.run([shutil.which('mkvalidator.exe'), f'{test_file_path!s}'],
                                       text=True, capture_output=True, check=True)
                        validate_result = ValidateResult.OK
                        output.total_bytes += test_file_path.stat().st_size
                    except subprocess.CalledProcessError:
                        validate_result = ValidateResult.FAILED

                    finally:
                        test_file_path.unlink()
                else:
                    validate_result = ValidateResult.NO_OUTPUT
                logger.log(logging.INFO if validate_result == ValidateResult.OK else logging.ERROR,
                           f'   {validate_result.name}')
                result_color = f'... {Fore.LIGHTGREEN_EX if validate_result == ValidateResult.OK else Fore.RED}'
                sys.stdout.write(f'{result_color}{validate_result.name}\n')

    sys.stdout.write(f'Total valid output: {gigabyte_string(output.total_bytes)}\n')
    sys.stdout.write("Finished.")
    output.elapsed_time = timer.format_delta()
    logging.info(f'Elapsed running time: {timer.format_delta()}')
    logging.info(f'Total bytes: {gigabyte_string(output.total_bytes)}')
    logging.info('Finished.')
    return output


def _logger_name() -> str:
    return 'validate_iso'


def main() -> int:
    parser = argparse.ArgumentParser(description='Scan ISO images with makemkv info')
    parser.add_argument('-o', '--output',  required=True, help='Output folder (default is input folder)', type=Path)
    parser.add_argument('-p', '--data-dict-path', required=True, help='Path of data dictionary pickle file.', type=Path)
    parser.add_argument('-t', '--temp-path', required=True, help="Temp file path")
    parser.add_argument('-f', '--force', help='Force scan even if already scanned', type=bool,
                        action=argparse.BooleanOptionalAction)
    parser.add_argument('--process-corrupt', help='Validate files with info errors',
                        action=argparse.BooleanOptionalAction,
                        default=True)
    parser.add_argument('--process-success', help='Validate files with no info errors',
                        action=argparse.BooleanOptionalAction,
                        default=True)
    program_args = ProgramArgs()

    parser.parse_args(namespace=program_args)
    validate_iso(program_args)
    return 0


if __name__ == "__main__":
    sys.exit(main())