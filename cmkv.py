import argparse
from pathlib import Path
import os
from media_util import gigabyte_string
import subprocess

def run_mkv(iso_file: Path, program_args) -> bool:

    mkv_args = ['makemkvcon.exe', '--messages=-null',  '--noscan', '--cache=2000', '--minlength=180',
                'mkv', f'iso:{str(iso_file)}', 'all', f'{str(program_args.output_path)}']

    print(f'Running makemkvcon.exe: {mkv_args}')

    result = 0

    if not program_args.dry_run:
        result = subprocess.run(mkv_args).returncode

    return result == 0

def folder_mkv_size(folder_name: Path) -> int:
    return sum([F.stat().st_size for F in folder_name.rglob('*.mkv')])

def process_iso(file: Path, program_args) -> bool:
    os.makedirs(str(program_args.output_path), exist_ok=True)
    result = run_mkv(file, program_args)

    iso_file_size = file.stat().st_size
    if result and not program_args.dry_run:
        output_files = [F for F in program_args.output_path.glob('*.mkv')]
        output_files = sorted(output_files, key =  lambda x: os.stat(str(x)).st_size)
        total_bytes = folder_mkv_size(program_args.output_path)
        if output_files:
            while total_bytes >= iso_file_size:
                output_files.pop().unlink()
                total_bytes = folder_mkv_size(program_args.output_path)
            assert output_files

    return result


def convert_mkv(program_args):
    print(f'Dry run: {program_args.dry_run}')
    print(f'Limit: {program_args.limit}')
    total_mkv_bytes = 0
    total_iso_bytes = 0
    has_errors = False

    program_args.input_path = Path(str(program_args.search_folder))
    for file in program_args.input_path.rglob('*.iso'):
        file_size = file.stat().st_size
        print(f'Processing ISO: {file.name}, ({gigabyte_string(file_size)})')
        media_stem = list(file.parts[len(program_args.input_path.parts):])
        media_stem[-1] = file.stem
        final_output_path = str(program_args.output if program_args.output else program_args.input)
        program_args.output_path=Path(final_output_path, *media_stem)

        result = process_iso(file, program_args)
        if not result:
            print(f'ERROR')
            has_errors = True
            break

        if total_iso_bytes >= program_args.limit * 1024*1024:
            break

        total_iso_bytes += file_size
        total_mkv_bytes += folder_mkv_size(program_args.output_path)

    print(f'Total ISO processed: {gigabyte_string(total_iso_bytes)}')
    print(f'Total MKV processed: {gigabyte_string(total_mkv_bytes)}')

    if has_errors:
        print('Finished with errors.')
    else:
        print('Finished! No errors.')

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Convert mkv files')
    parser.add_argument('-d', '--dry-run', action='store_true', default=False, help='Dry run')
    parser.add_argument('-m', '--limit', type=int, help='Processing limit in gigabytes.', default=1000)
    parser.add_argument('-o', '--output', type=str, help='Output folder. If specified, input ISO files will not be deleted')
    parser.add_argument('search_folder', type=str, help='Search folder')
    args = parser.parse_args()
    convert_mkv(args)