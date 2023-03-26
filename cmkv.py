import argparse
from pathlib import Path
from media_util import gigabyte_string

def process_iso(file: Path):
    print(f'ISO file found: {file.name}')

def convert_mkv(program_args):
    print(f'Dry run: {program_args.dry_run}')
    print(f'Limit: {program_args.limit}')
    for file in Path(program_args.input).rglob('*.iso'):
        process_iso(file)
    print('Finished!')

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Convert mkv files')
    parser.add_argument('-d', '--dry-run', action='store_true', default=False, help='Dry run')
    parser.add_argument('-m', '--limit', type=int, help='Processing limit in gigabytes.', default=2000)
    parser.add_argument('input', type=str, help='input folders', nargs='+')
    args = parser.parse_args()
    convert_mkv(args)