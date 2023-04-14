from contextlib import contextmanager
import time
import logging

def setup_logging(log_file_name: str):
    log_format = '%(asctime)s %(levelname)s: %(message)s'
    console_format = '%(levelname)-8s %(message)s'

    if log_file_name is None:
        logging.basicConfig(format=console_format,
                            encoding='utf-8',
                            level=logging.INFO)
    else:
        logging.basicConfig(format=log_format,
                            filename=log_file_name,
                            encoding='utf-8',
                            filemode='w',
                            datefmt='%m-%d %H:%M',
                            level=logging.DEBUG)

        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        # set a format which is simpler for console use
        formatter = logging.Formatter(console_format)
        console.setFormatter(formatter)
        # add the handler to the root logger
        logging.getLogger().addHandler(console)

def gigabyte_string(n: int) -> str:
    one_kb = 1024
    if n >= one_kb ** 3:
        return f'{n * float(one_kb ** -3):.2f} GB'
    elif n >= one_kb ** 2:
        return f'{n * float(one_kb ** -2):.2f} MB'
    else:
        return f'{n * float(one_kb ** -1):.2f} KB'


@contextmanager
def timed_method(start_message: str=''):
    def format_time_delta(value) -> str:  # time() - start_time
        hours, remainder = divmod(value, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f'{int(hours):02}:{int(minutes):02}:{int(seconds):02}'

    if start_message:
        logging.info(start_message)

    time_start = time.time()
    yield
    logging.getLogger().info(f"Elapsed time: {format_time_delta(time.time()-time_start)}")