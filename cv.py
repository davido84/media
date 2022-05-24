import argparse
from pathlib import Path
from cv_args import ConvertVideoArgs
import sys


def cv(program_args: ConvertVideoArgs) -> None:
    pass


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument('input_folder', help='Input folder', metavar='<input folder>', type=Path)
    parser.add_argument('-f', '--force', help='Process all files.', action=argparse.BooleanOptionalAction,
                        default=False)

    parser.add_argument('-m', '--minlength', dest='min_title_length', metavar='MIN_TITLE_LENGTH_IN_SECONDS', type=int,
                        help='Minimum title length in seconds.', default=60*3)

    parser.add_argument('-r', '--filter', dest='file_filter', metavar='FILTER',
                        help='Input file filter. Example: "*d1*.*', default=None)

    parser.add_argument('-t', '--temp-folder', help='Temp folder, Default: "T:/"', type=Path, default=Path('T:/'))

    parser.add_argument('-D', '--database-folder', help='Database Folder', type=Path, default=Path('.'))

    levels = ('DEBUG', 'INFO', 'WARNING', 'ERROR')
    parser.add_argument('--log-level', default='INFO', help='Logging level', metavar='LOG_LEVEL', choices=levels)

    parser.add_argument('-L', '--log-file', help='Log file path', metavar='LOG_FILE_PATH', default=None, type=Path)

    subparsers = parser.add_subparsers(help='[scan | validate]', title='Subcommands',
                                       dest='subcommand_name', metavar='[scan | validate]', required=True)
    scan_parser = subparsers.add_parser('scan')
    scan_parser.add_argument('-e', '--timeout', help='Timeout in seconds', metavar='TIMEOUT_IN_SECONDS',
                             type=int, default=60*4)

    validate_parser = subparsers.add_parser('validate')

    if len(sys.argv) == 1:
        parser.print_usage()
        parser.exit(0)

    args = parser.parse_args()
    program_args = ConvertVideoArgs(**vars(args))

    # cv(program_args)


if __name__ == "__main__":
    main()