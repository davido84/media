import argparse
import logging
from pathlib import Path
from mkvlib import mkv_info

def _setup_logging(log_file_name: str):
    if log_file_name is not None:
        logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s',
                            filename=str(log_file_name),
                            encoding='utf-8',
                            filemode='w',
                            datefmt='%m-%d %H:%M',
                            level=logging.DEBUG)

    # define a Handler which writes INFO messages or higher to the sys.stderr
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    # set a format which is simpler for console use
    formatter = logging.Formatter('%(levelname)-8s %(message)s')
    # tell the handler to use this formatpy
    console.setFormatter(formatter)
    # add the handler to the root logger
    logging.getLogger().addHandler(console)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Convert Video Files')
    parser.add_argument('-i', '--input', type=str, help='Input folder or file')
    parser.add_argument('-o', '--output', type=str, help='Output folder')
    parser.add_argument('-g', '--logfile', type=str, help='Log file name', default=None)

    subparsers = parser.add_subparsers(title='Commands', description='Convert Video Commands')
    parser_filename = subparsers.add_parser('mkv-info', description='Run mkv info', aliases=['mi'])

    parser_filename.set_defaults(func=mkv_info)
    args = parser.parse_args()
    _setup_logging(args.logfile)

    args.func(args)