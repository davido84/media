import argparse
from pathlib import Path
import os
from media_util import gigabyte_string
import subprocess
import logging

def run_mkv(iso_file: Path, program_args) -> bool:

    mkv_args = ['makemkvcon.exe', '--messages=-null',  '--noscan', '--cache=2000', '--minlength=180',
                'mkv', f'iso:{str(iso_file)}', 'all', f'{str(program_args.output_path)}']

    cmd = ''.join([f'"{S}" ' for S in mkv_args]).strip()
    logging.getLogger().info(f'Running makemkvcon.exe: {cmd}')

    result = 0

    if not program_args.dry_run:
        result = subprocess.run(mkv_args).returncode

    return result == 0

def folder_mkv_size(folder_name: Path) -> int:
    return sum([F.stat().st_size for F in folder_name.rglob('*.mkv')])

def process_iso(file: Path, program_args) -> bool:
    os.makedirs(str(program_args.output_path), exist_ok=True)
    result = run_mkv(file, program_args)

    if result and not program_args.dry_run:
        iso_file_size = file.stat().st_size
        output_files = [F for F in program_args.output_path.glob('*.mkv')]
        output_files = sorted(output_files, key =  lambda x: os.stat(str(x)).st_size)
        total_bytes = folder_mkv_size(program_args.output_path)
        if output_files:
            while total_bytes >= iso_file_size:
                output_files.pop().unlink()
                total_bytes = folder_mkv_size(program_args.output_path)
            assert output_files

        file_sizes: set[int] = set()
        for file in output_files:
            file_size = file.stat().st_size
            if file_size in file_sizes:
                file.unlink()
            else:
                file_sizes.add(file_size)

    return result


def convert_mkv(program_args):
    logger = logging.getLogger()
    logger.info('Starting MKV conversion.')

    logger.info(f'Dry run: {program_args.dry_run}')
    logger.info(f'Limit: {program_args.limit}')
    total_mkv_bytes = 0
    total_iso_bytes = 0
    has_errors = False

    program_args.input_path = Path(str(program_args.search_folder))
    for file in program_args.input_path.rglob('*.iso'):
        file_size = file.stat().st_size
        logger.info(f'Processing ISO: "{str(file)}", ({gigabyte_string(file_size)})')
        media_stem = list(file.parts[len(program_args.input_path.parts):])
        media_stem[-1] = file.stem
        final_output_path = str(program_args.output if program_args.output else program_args.input)
        program_args.output_path=Path(final_output_path, *media_stem)

        result = process_iso(file, program_args)
        if not result:
            logger.critical(f'ERROR')
            has_errors = True
            break

        if total_iso_bytes >= program_args.limit * 1024*1024:
            break

        total_iso_bytes += file_size
        total_mkv_bytes += folder_mkv_size(program_args.output_path)

    logger.info(f'Total ISO processed: {gigabyte_string(total_iso_bytes)}')
    logger.info(f'Total MKV processed: {gigabyte_string(total_mkv_bytes)}')

    if has_errors:
        logger.info('Finished with errors.')
    else:
        logger.info('Finished! No errors.')

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Convert mkv files')
    parser.add_argument('-d', '--dry-run', action='store_true', default=False, help='Dry run')
    parser.add_argument('-m', '--limit', type=int, help='Processing limit in gigabytes.', default=1000)
    parser.add_argument('-o', '--output', type=str,
                        help='Output folder. If specified, input ISO files will not be deleted')
    parser.add_argument('search_folder', type=str, help='Search folder')
    args = parser.parse_args()

    log_file_name = Path(args.search_folder, 'mkv-log.txt')

    logging.basicConfig(format='%(levelname)s: %(message)s',
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

    convert_mkv(args)