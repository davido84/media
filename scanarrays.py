from scaniso import ProgramArgs, scan_iso
from pathlib import Path
import argparse
from mediautil import Timer
from mediautil import gigabyte_string


def validate_array(force: bool):
    settings = ProgramArgs()
    settings.timeout = 60 * 5
    settings.minlength = 3 * 60
    settings.force = force

    settings.input = Path('d:/movies')
    settings.output = settings.input

    output = scan_iso(settings)

    # for disc in range(1, 10):
    #     settings.input_folder = Path(f'c:/mnt/d{disc}/array.{disc}')
    #     settings.output_folder = Path(f'c:/mnt/d{disc}')
    #
    #     validate_iso(settings)

    print(f'Max file size: {gigabyte_string(output.max_file_size)}')


def main():
    parser = argparse.ArgumentParser('Check all disc arrays')
    parser.add_argument('-f', '--force', help='Force scan of all files.', action=argparse.BooleanOptionalAction,
                        default=False)
    args = parser.parse_args()

    with Timer(verbose=True):
        validate_array(args.force)


if __name__ == '__main__':
    main()