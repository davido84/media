import argparse
from pathlib import Path
from media_util import gigabyte_string

def remove_dup(program_args):
    print(f'Dry run: {program_args.dry_run}')
    print('Input folders:')
    for input_folder in program_args.input:
        print(str(input_folder))
    print('Finished!')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Remove duplicate files')
    parser.add_argument('-d', '--dry-run', action='store_true', default=False, help='Dry run')
    parser.add_argument('input', type=str, help='input folders', nargs='+')
    args = parser.parse_args()
    remove_dup(args)