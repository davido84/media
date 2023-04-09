import argparse
from pathlib import Path
import os
from media_util import gigabyte_string, timed_method
import subprocess
import logging
import sys
import re
from rmfolders import remove_empty_folders
from collections import defaultdict
import shutil

_MESSAGE_HEADER='='*20
_MIN_SECONDS=60*3

# TINFO:0,11,0,"2217588736"
# TINFO:0,9,0,"0:44:51"
# TINFO:2,16,0,"00008.mpls"
# TINFO:2,26,0,"505,501,508,507,512,520,504,513,517,510,506,515,518,516,503,511,519,502,514,509"

# Two big titles of the same size: Second one is Japanese, use the first one
_TITLE_MPLS_RE = re.compile(r'^TINFO:(\d+),16,\d+,"(\d+\.mpls)"')
_TITLE_SIZE_RE = re.compile(r'^TINFO:(\d+),11,\d+,"(\d+)"')
_TITLE_LENGTH_RE = re.compile(r'^TINFO:(\d+),\d+,\d+,"(\d+):(\d+):(\d+)"')
_TITLE_SEGMENT_MAP_RE = re.compile(r'TINFO:(\d+),26,0,"(\d+),\d+.*"') # More than segment indicates scrambling


def _scan_titles(iso_file: Path) ->set[int]:
    def seconds(hours: int, minutes: int, sec: int) -> int:
        return sec + minutes * 60 + hours * 60 * 60

    titles_to_convert: set[int] = set()

    output_lines = [L.strip() for L in subprocess.run(
        ['makemkvcon64.exe', '-r', 'info', str(iso_file)],
        check=True, capture_output=True).stdout.decode('utf-8').split('\n')]

    title_mpls: list[(int, str)] = [(int(M.group(1)), M.group(2)) for M in
                                    [_TITLE_MPLS_RE.match(L) for L in output_lines] if M is not None ]

    title_sizes: list[(int, int, str)] = [(int(M.group(1)), int(M.group(2)), gigabyte_string(int(M.group(2)))) for M in
                                    [_TITLE_SIZE_RE.match(L) for L in output_lines] if M is not None ]

    title_lengths: list[(int,int)] = [(int(M.group(1)),
                                                seconds(int(M.group(2)), int(M.group(3)), int(M.group(4)) ))
                                      for M in [_TITLE_LENGTH_RE.match(L) for L in output_lines] if M is not None]

    all_titles: list[(int,int,int)] = [(T[0][0],T[0][1], T[1][1]) for T in
        list(zip(title_sizes, title_lengths)) if T[1][1] >= _MIN_SECONDS]

    # John Wick correct: Source file name: 00641.mpls
    # 505,508,507,501,512,504,520,513,517,506,515,518,510,516,503,511,519,502,514,509
    # Remove large "Play All" titles
    return titles_to_convert


def _mkv_files(input_path: Path) ->list[Path]:
    return sorted([F for F in input_path.glob('*.mkv')],  key = lambda x: os.stat(str(x)).st_size)


def _total_mkv_size(input_path: Path) ->int:
    return sum(F.stat().st_size for F in _mkv_files(input_path))


def _run_mkv(iso_file: Path, output_path: Path, program_args) ->None:
    mkv_args = ['makemkvcon64.exe','--messages=-stdout', '--noscan', '--cache=2000', '--minlength=180',
                'mkv', f'iso:{str(iso_file)}', 'all', f'{str(output_path)}'+'\\']

    cmd = ''.join([f'"{S}" ' for S in mkv_args]).strip()
    logging.debug(f'{cmd}')

    subprocess.run(mkv_args, check=True, capture_output=True)


def _process_iso(input_file: Path, program_args) ->None:
    _scan_titles(input_file)

    media_stem = [L.strip() for L in list(input_file.parts[len(program_args.search_folder.parts):])]
    media_stem[-1] = input_file.stem
    output_path = Path(program_args.output_folder, *media_stem)
    if program_args.force or not output_path.exists():
        if output_path.exists():
            shutil.rmtree(str(output_path))

        logging.debug(f'Output path: {str(output_path)}')
        os.makedirs(str(output_path), exist_ok=True)
        _run_mkv(input_file, output_path, program_args)
        iso_file_size = input_file.stat().st_size

        # Remove larges files if the total converted size is > than
        # the original sio file size
        mkv_files = _mkv_files(output_path)
        while mkv_files and _total_mkv_size(output_path) > iso_file_size:
            file_to_remove: Path = mkv_files.pop()
            logging.debug(
                f'[R] Removing PLAY ALL title: {str(file_to_remove)} - {gigabyte_string(file_to_remove.stat().st_size)}')
            file_to_remove.unlink()

        # Remove any duplicate files
        file_size_set: set[int] = set()
        for iso_file in _mkv_files(output_path):
            file_size = iso_file.stat().st_size
            if file_size in file_size_set:
                logging.debug(f'[D] Removing DUPLICATE title: {str(iso_file)}')
                iso_file.unlink()
            else:
                file_size_set.add(file_size)

        program_args.total_iso_bytes += iso_file_size
        program_args.total_mkv_bytes += _total_mkv_size(output_path)
    else:
        logging.debug(f'Skipping existing output.')


def convert_mkv(program_args):
    logging.info('Starting MKV conversion.')

    logging.info(f'Limit: {gigabyte_string(program_args.limit)}') # limit bytes to process in GB

    for counter, iso_file in enumerate(Path(program_args.search_folder).rglob('*.iso')):
        iso_file_size = iso_file.stat().st_size
        logging.debug(f'{_MESSAGE_HEADER} "{str(iso_file)}" : {counter} {_MESSAGE_HEADER}')
        logging.info(f'"{str(iso_file)}", ({gigabyte_string(iso_file_size)})')
        _process_iso(iso_file, program_args)

        if program_args.total_iso_bytes > program_args.limit:
            logging.info(f'Exiting after processing {gigabyte_string(program_args.total_iso_bytes)}')
            break

        if program_args.delete:
            iso_file.unlink()
            logging.debug(f'Removed file: {iso_file}')


def _check_iso_playlist(iso_file: Path) ->bool:
    output_lines = [L.strip() for L in subprocess.run(
        ['makemkvcon64.exe', '-r', 'info', str(iso_file)],
        check=True, capture_output=True).stdout.decode('utf-8').split('\n')]

    title_size_lines: list[(int, int)] = [(int(M.group(1)), int(M.group(2))) for M in
                                    [_TITLE_SIZE_RE.match(L) for L in output_lines]
                                          if M is not None and int(M.group(2)) > 1024*1024*1024]

    title_size_dict: defaultdict[int, set[int]] = defaultdict(set)
    for T in title_size_lines:
        title_size_dict[T[1]].add(T[0])

    # has_multiple_segments: bool =  not any([_TITLE_SEGMENT_MAP_RE.match(L) for L in output_lines])
    max_same_size_titles: int = max(len(T) for T in title_size_dict.values())

    return max_same_size_titles <= 2


def check_playlists(program_args):
    num_checked=0
    error_files: list[Path] = []
    for iso_file in Path(program_args.search_folder).rglob('*.iso'):
        print(f'{str(iso_file)}...', flush=True, end='')
        if _check_iso_playlist(iso_file):
            print('OK')
        else:
            print('SCRAMBLED')
            error_files.append(iso_file)
        num_checked += 1

    print(f'{num_checked} files checked.')
    print(f'{len(error_files)} error(s), {num_checked-len(error_files)} okay.')

    for file in error_files:
        logging.debug(str(file))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Extract mkv files.')
    parser.add_argument('-m', '--limit', type=int, help='Processing limit in gigabytes.', default=100)
    parser.add_argument('-d', '--delete', help='Delete input .iso file.', action='store_true', default=False)
    parser.add_argument('-f', '--force', help='Overwrite output files', action='store_true', default=False)
    parser.add_argument('search_folder', type=str, help='Search folder')
    parser.add_argument('output_folder', help='Output folder', type=str)
    args = parser.parse_args()
    args.search_folder = Path(args.search_folder)
    args.output_folder = args.output_folder.strip()

    os.makedirs(str(args.output_folder), exist_ok=True)
    # log_file_name = Path(args.output_folder, 'makemkv-log.txt')
    log_file_name = Path(args.search_folder, 'check-playlists.txt')

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
    # tell the handler to use this format
    console.setFormatter(formatter)
    # add the handler to the root logger
    logging.getLogger().addHandler(console)

    check_playlists(args)
    # try:
    #     args.total_mkv_bytes = 0
    #     args.total_iso_bytes = 0
    #
    #     with timed_method():
    #         args.limit *= 1024*1024*1024
    #         convert_mkv(args)
    #         logging.debug(f'{_MESSAGE_HEADER} Remove Empty Folders {_MESSAGE_HEADER}')
    #         remove_empty_folders(str(args.search_folder))
    #         logging.debug(f'{_MESSAGE_HEADER} Summary {_MESSAGE_HEADER}')
    #         logging.info(f'ISO bytes processed: {gigabyte_string(args.total_iso_bytes)}')
    #         logging.info(f'MKV bytes processed: {gigabyte_string(args.total_mkv_bytes)}')
    #         logging.info(f'Reduction: {gigabyte_string(args.total_iso_bytes - args.total_mkv_bytes)}')
    #         logging.info('Finished!')
    # except IOError as e:
    #     logging.critical(f'IO Error: {str(e)}')
    # except subprocess.CalledProcessError as e:
    #     logging.critical(f'makemkvcon error')
    #     logging.critical(e.output.decode('utf-8'))