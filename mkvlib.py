import logging
from pathlib import Path
import subprocess

# _ENCODING = 'ISO-8859-1'

def mkv_info(program_args):
    # logging.info('MKV info')
    input_path = Path(program_args.input)
    # logging.info(f'Input path: {str(input_path)}')
    error_files = []

    for iso_file in input_path.rglob('*.iso'):
        print(f'{str(iso_file)}...', end='')
        output_file = iso_file.with_suffix('.info')
        output = subprocess.run(['makemkvcon64.exe', '--noscan', '-r', '--cache=2000', '--minlength=0',
                                 'info', str(iso_file)], check=True, capture_output=True)
        try:
            output.stdout.decode('utf-8')
            print('OK')
        except UnicodeDecodeError:
            error_files.append(iso_file)
            print('ERROR')

        if not program_args.check:
            pass
            # output_file.write_text('\n'.join([L for L in file_lines if re.match(r'^[TCS]INFO', L)]), encoding='utf-8')

    print(f'Finished - {len(error_files)} error(s).')

    for file in error_files:
        logging.error(str(file))