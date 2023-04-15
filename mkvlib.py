import logging
from pathlib import Path
import subprocess
import re

_ENCODING = 'ISO-8859-1'

def mkv_info(program_args):
    if program_args.input is None:
        print('Missing input/output paths.')
        return

    logging.info('MKV info')
    input_path = Path(program_args.input)
    logging.info(f'Input path: {str(input_path)}')

    for iso_file in Path(program_args.input).rglob('*.iso'):
        logging.info(str(iso_file))
        output_file = iso_file.with_suffix('.info')
        output = subprocess.run(['makemkvcon64.exe', '--noscan', '-r', '--cache=2000', '--minlength=0',
                                 'info', str(iso_file)], check=True, capture_output=True)
        # file_lines = output.stdout.decode('utf-8').split('\r\n')
        # output_file.write_text('\n'.join([L for L in file_lines if re.match(r'^[TCS]INFO', L)]), encoding='utf-8')

    logging.info('Finished!')