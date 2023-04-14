import argparse
import logging
from pathlib import Path
from mkvlib import mkv_info
from media_util import setup_logging


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Convert Video Files')
    parser.add_argument('-i', '--input', type=str, help='Input folder or file')
    parser.add_argument('-o', '--output', type=str, help='Output folder')
    parser.add_argument('-g', '--logfile', type=str, help='Log file name', default=None)

    subparsers = parser.add_subparsers(title='Commands', description='Convert Video Commands')
    parser_filename = subparsers.add_parser('mkv-info', description='Run mkv info', aliases=['mi'])

    parser_filename.set_defaults(func=mkv_info)
    args = parser.parse_args()
    setup_logging(args.logfile)

    args.func(args)