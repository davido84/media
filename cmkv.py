import argparse
from pathlib import Path
import os
from media_util import gigabyte_string
import subprocess
import logging
import shutil

def _run_mkv(iso_file: Path, output_path: Path, program_args) ->None:

    mkv_args = ['makemkvcon64.exe','--messages=-stdout', '--noscan', '--cache=2000', '--minlength=180',
                'mkv', f'iso:{str(iso_file)}', 'all', f'{str(output_path)}']

    cmd = ''.join([f'"{S}" ' for S in mkv_args]).strip()
    logging.getLogger().info(f'Running: {cmd}')

    if program_args.dry_run:
        output_file = Path(program_args.output_path, 'title.mkv')
        logging.getLogger().info(f'Will create file: str{output_file}')
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

    # def folder_mkv_size(folder_name: Path) -> int:
    #     return sum([F.stat().st_size for F in folder_name.rglob('*.mkv')])
    #
    # os.makedirs(str(program_args.output_path), exist_ok=True)
    # result = _run_mkv(file, program_args)
    #
    # if result and not program_args.dry_run:
    #     iso_file_size = file.stat().st_size
    #     output_files = sorted([F for F in program_args.output_path.glob('*.mkv')],
    #                           key =  lambda x: os.stat(str(x)).st_size)
    #
    #     total_bytes = folder_mkv_size(program_args.output_path)
    #
    #     if output_files:
    #         while total_bytes >= iso_file_size:
    #             output_files.pop().unlink()
    #             total_bytes = folder_mkv_size(program_args.output_path)
    #         assert output_files
    #
    #     file_sizes: set[int] = set()
    #     for file in output_files:
    #         file_size = file.stat().st_size
    #         if file_size in file_sizes:
    #             file.unlink()
    #         else:
    #             file_sizes.add(file_size)
    #
    # return result


def convert_mkv(program_args):
    logger = logging.getLogger()
    logger.info('Starting MKV conversion.')

    logger.info(f'Dry run: {program_args.dry_run}')
    logger.info(f'Limit: {program_args.limit}') # limit bytes to process in GB

    for file in Path(program_args.search_folder).rglob('*.iso'):
        iso_file_size = file.stat().st_size
        logger.info(f'Processing ISO: "{str(file)}", ({gigabyte_string(iso_file_size)})')
        _process_iso(file, program_args)


        # logger.debug(f'Output path: str({output_path}')
        # result = _process_iso(file, )
        #
        # result = process_iso(file, program_args)
        # if not result:
        #     logger.critical(f'ERROR')
        #     has_errors = True
        #     break
        #
        # if total_iso_bytes >= program_args.limit * 1024*1024:
        #     break
        #
        # total_iso_bytes += file_size
        # total_mkv_bytes += folder_mkv_size(program_args.output_path)

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

        convert_mkv(args)
        logging.getLogger().info(f'ISO data processed: {gigabyte_string(args.total_iso_bytes)}')
        logging.getLogger().info(f'MKV data processed: {gigabyte_string(args.total_mkv_bytes)}')
        logging.getLogger().info('Finished!')
    except IOError as e:
        logging.getLogger().critical(f'IO Error: {str(e)}')
    except subprocess.CalledProcessError as e:
        logging.getLogger().critical(f'makemkvcon error')
        logging.getLogger().critical(e.output.decode('utf-8'))