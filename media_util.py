from contextlib import contextmanager
import time
import logging

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