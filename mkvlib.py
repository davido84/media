import logging
from pathlib import Path

def mkv_info(program_args):
    logging.info('Starting mkv info')
    input_path = Path(program_args.input)
    output_path = Path(program_args.output)
    logging.info(f'Import path: {str(input_path)}')
    logging.info(f'Output path: {str(output_path)}')
    logging.debug('This is a debug message')