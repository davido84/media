import argparse
import sys
from pathlib import Path
from mkvlib import mkv_info
from media_util import setup_logging

def main():
    parser = argparse.ArgumentParser(description='Convert Video File Tools')
    parser.add_argument('-i', '--input', type=str, help='Input folder or file')
    parser.add_argument('-o', '--output', type=str, help='Output folder')
    parser.add_argument('--log', action='store_true', default=False, help='Create log file')
    parser.add_argument('--log-name', type=str, help='Logfile name', default='cv.log')

    subparsers = parser.add_subparsers(title='Commands', description='Commands')

    # mkv-info Command
    parser_filename = subparsers.add_parser('mkv-info', description='Run mkv info', aliases=['mi'])
    parser_filename.add_argument('--check', action='store_true', default=False,
                                 help='Check that mke-info can run without errors.')
    parser_filename.set_defaults(func=mkv_info)

    args = parser.parse_args()

    if args.input is None:
        print('Missing --input')
        return -1

    setup_logging(str(Path(args.input,args.log_name)) if args.log else None)

    args.func(args)
    return 0

if __name__ == '__main__':
    sys.exit(main())