import argparse
from pathlib import Path
import os
from media_util import gigabyte_string, timed_method
import subprocess
import logging
import shutil

def _mkv_files(input_path: Path) ->list[Path]:
    return sorted([F for F in input_path.glob('*.mkv')],  key = lambda x: os.stat(str(x)).st_size)

def _total_mkv_size(input_path: Path) ->int:
    return sum(F.stat().st_size for F in _mkv_files(input_path))

def _run_mkv(iso_file: Path, output_path: Path, program_args) ->None:

    mkv_args = ['makemkvcon64.exe','--messages=-stdout', '--noscan', '--cache=2000', '--minlength=180',
                'mkv', f'iso:{str(iso_file)}', 'all', f'{str(output_path)}'+'\\']

    cmd = ''.join([f'"{S}" ' for S in mkv_args]).strip()
    logging.getLogger().debug(f'Running: {cmd}')

    if program_args.dry_run:
        output_file = Path(program_args.output_path, 'title.mkv')
        logging.getLogger().debug(f'Will create file: str{output_file}')
    else:
        subprocess.run(mkv_args, check=True, capture_output=True)

def _process_iso(input_file: Path, program_args) ->None:
    media_stem = list(input_file.parts[len(program_args.search_folder.parts):])
    media_stem[-1] = input_file.stem
    output_path = Path(program_args.output_folder, *media_stem)
    if program_args.force or not output_path.exists():
        if output_path.exists():
            shutil.rmtree(str(output_path))

        logging.getLogger().debug(f'Output path: {str(output_path)}')
        os.makedirs(str(output_path), exist_ok=True)

        _run_mkv(input_file, output_path, program_args)
        iso_file_size = input_file.stat().st_size

        # Remove larges files if the total converted size is > than
        # the original sio file size
        mkv_files = _mkv_files(output_path)
        while mkv_files and _total_mkv_size(output_path) > iso_file_size:
            file_to_remove: Path = mkv_files.pop()
            logging.getLogger().debug(
                f'Removing: {str(file_to_remove)} - {gigabyte_string(file_to_remove.stat().st_size)}')
            file_to_remove.unlink()

        # Remove any duplicate files
        file_size_set: set[int] = set()
        for file in _mkv_files(output_path):
            file_size = file.stat().st_size
            if file_size in file_size_set:
                logging.getLogger().debug(f'Removing duplicate: {str(file)}')
                file.unlink()
            else:
                file_size_set.add(file_size)

        program_args.total_iso_bytes += iso_file_size
        program_args.total_mkv_bytes += _total_mkv_size(output_path)

def convert_mkv(program_args):
    logger = logging.getLogger()
    logger.info('Starting MKV conversion.')

    logger.info(f'Dry run: {program_args.dry_run}')
    logger.info(f'Limit: {program_args.limit}') # limit bytes to process in GB

    for file in Path(program_args.search_folder).rglob('*.iso'):
        iso_file_size = file.stat().st_size
        logger.info(f'Processing ISO: "{str(file)}", ({gigabyte_string(iso_file_size)})')
        _process_iso(file, program_args)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Convert mkv files')
    parser.add_argument('-y', '--dry-run', action='store_true', default=False, help='Dry run')
    parser.add_argument('-m', '--limit', type=int, help='Processing limit in gigabytes.', default=1000)
    parser.add_argument('-d', '--delete', help='Delete input .iso file.', action='store_true', default=False)
    parser.add_argument('-f', '--force', help='Overwrite output files', action='store_true', default=False)
    parser.add_argument('search_folder', type=str, help='Search folder')
    parser.add_argument('output_folder', help='Output folder', type=str)
    args = parser.parse_args()
    args.search_folder = Path(args.search_folder)
    os.makedirs(str(args.output_folder), exist_ok=True)
    log_file_name = Path(args.output_folder, 'makemkv-log.txt')

    logging.basicConfig(format=' %(levelname)s: %(message)s',
                        filename=str(log_file_name),
                        encoding='utf-8',
                        filemode='w',
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

    try:
        args.total_mkv_bytes = 0
        args.total_iso_bytes = 0

        with timed_method():
            convert_mkv(args)
            logging.getLogger().info(f'ISO data processed: {gigabyte_string(args.total_iso_bytes)}')
            logging.getLogger().info(f'MKV data processed: {gigabyte_string(args.total_mkv_bytes)}')
            logging.getLogger().info(f'Delta: {gigabyte_string(args.total_iso_bytes - args.total_mkv_bytes)}')
            logging.getLogger().info('Finished!')
    except IOError as e:
        logging.getLogger().critical(f'IO Error: {str(e)}')
    except subprocess.CalledProcessError as e:
        logging.getLogger().critical(f'makemkvcon error')
        logging.getLogger().critical(e.output.decode('utf-8'))