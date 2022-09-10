from contextlib import contextmanager
import time


def gigabyte_string(n: int) -> str:
    one_kb = 1024
    if n >= one_kb ** 3:
        return f'{n * float(one_kb ** -3):.2f} GB'
    elif n >= one_kb ** 2:
        return f'{n * float(one_kb ** -2):.2f} MB'
    else:
        return f'{n * float(one_kb ** -1):.2f} KB'


def format_time_delta(value) -> str:  # time() - start_time
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{int(hours):02} hours, {int(minutes):02} minutes, {int(seconds):02} seconds'


@contextmanager
def media_method(start_message: str):
    # log.info(start_message)
    # click.secho(start_message, fg='green', bold=True)
    time_start = time.time()
    try:
        yield
        # log.info('Finished!')
        # click.secho('Finished!', fg='green', bold=True)
        time_end = time.time()
        hours, rem = divmod(time_end - time_start, 3600)
        minutes, seconds = divmod(rem, 60)
        # click.secho(f'Elapsed time: {int(hours):0>2}:{int(minutes):0>2}:{seconds:05.2f}', fg='green', bold=True)
        # log.info("Elapsed time: {:0>2}:{:0>2}:{:05.2f}".format(int(hours), int(minutes), seconds))

    except MediaException as e:
        pass
        # click.secho(f'Media Error: {e}', fg='red', bold=True)


class MediaException(Exception):
    pass