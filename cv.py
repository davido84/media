import argparse
from pathlib import Path
from cv_args import ConvertVideoArgs
import sys
import importlib
import logging
from pprint import pformat
import os
from mediautil import Timer


def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    # parser.add_argument('input_folder', help='Input folder', metavar='INPUT_FOLDER', type=Path)

    parser.add_argument('-f', '--force', help='Process all files.', action=argparse.BooleanOptionalAction,
                        default=False)

    parser.add_argument('--filter', dest='filename_filter', metavar='FILENAME_FILTER',
                        help='Input file filter. Example: "*d1*.*', default=None)

    parser.add_argument('--log', help='Create log file.', action=argparse.BooleanOptionalAction,
                        default=True)

    parser.add_argument('--yaml', help='Create YAML file.', action=argparse.BooleanOptionalAction,
                        default=True)

    levels = ('DEBUG', 'INFO', 'WARNING', 'ERROR')
    parser.add_argument('--log-level', default='INFO', metavar='LOG_LEVEL',
                        help='Logging level for log file.', choices=levels)

    subparsers = parser.add_subparsers(help='Available commands', dest='command')

    scan_cmd = subparsers.add_parser('scan', help='Scan media files and create database.',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    scan_cmd.add_argument('-e', '--timeout', help='Timeout in seconds', metavar='TIMEOUT_IN_SECONDS',
                          type=int, default=60*4)

    validate_cmd = subparsers.add_parser('validate', aliases=['val'],
                                         help='Validate ISO files in the database.',
                                         formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    validate_cmd.add_argument('-t', '--temp-folder', help='Temp folder', type=Path, default=Path('T:/'))
    validate_cmd.add_argument('-s', '--status', choices=('valid', 'corrupt', 'all'),
                              help='Filter by status', default='all', dest='validate_status')

    mkv_cmd = subparsers.add_parser('makemkv', help='Convert ISO files to lossless MKV in place.',
                                    formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    mkv_cmd.add_argument('database_folder', help='Database folder', metavar='DATABASE_FOLDER', type=Path)

    convert_cmd = subparsers.add_parser('handbrake', aliases=['hb'],
                                        help='Convert mkv files and add to output library.',
                                        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    convert_cmd.add_argument('output_folder', help='Output folder', metavar='OUTPUT_FOLDER', type=Path)

    if len(sys.argv) == 1:
        parser.print_help()
        parser.exit(0)

    options = parser.parse_args()
    program_args = ConvertVideoArgs(**vars(options))
    root_logger = logging.getLogger()
    os.makedirs(str(program_args.database_folder), exist_ok=True)
    if program_args.output_folder is not None:
        os.makedirs(str(program_args.output_folder))

    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%m/%d %I:%M %p',
                        stream=sys.stdout)

    root_logger.info(f'Starting {program_args.command.upper()}')
    if program_args.log:
        file_formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='%m/%d %I:%M %p')
        log_file_name = Path(program_args.database_folder, f'{program_args.command}.log')
        file_handler = logging.FileHandler(encoding='utf-8', filename=f'{log_file_name}', mode='w')
        file_handler.setLevel(program_args.log_level)
        file_handler.setFormatter(file_formatter)

        root_logger.addHandler(file_handler)

    root_logger.info('%s', pformat(program_args))

    mod = importlib.import_module(options.command)
    cmd = getattr(mod, 'command')

    with Timer() as timer:
        cmd(program_args)

    root_logger.info(f'Elapsed time: {timer.format_delta()}')
    root_logger.info('Finished!')


if __name__ == "__main__":
    main()