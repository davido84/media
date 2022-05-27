import psutil
import os
from pathlib import Path
from timeit import default_timer
from datetime import timedelta
import math
import click
import subprocess
import shutil
import threading
import logging
from dataclasses import dataclass, field
from contextlib import contextmanager


@contextmanager
def media_command(name: str) -> None:
    logging.info(f'Starting {name}')
    start = default_timer()
    try:
        yield
    finally:
        end = default_timer()
        elapsed_secs = (end - start)
        logging.info(f'Elapsed time: {str(timedelta(seconds=math.floor(elapsed_secs)))}')
        logging.info('Finished.')


def gigabytes(num_gb: int) -> int:
    return num_gb*1000*1000*1000


def gigabyte_string(n: int) -> str:
    one_kb = 1024
    if n >= one_kb ** 3:
        return '{:.2f}Gb'.format(n * float(one_kb ** -3))
    elif n >= one_kb ** 2:
        return '{:.2f}Mb'.format(n * float(one_kb ** -2))
    else:
        return '{:.2f}Kb'.format(n * float(one_kb ** -1))


def set_priority(pid=None, priority=1):
    """ Set The Priority of a Windows Process.  Priority is a value between 0-5 where
        2 is normal priority.  Default sets the priority of the current
        python process but can take any valid process ID. """

    priority_classes = [psutil.IDLE_PRIORITY_CLASS,
                        psutil.BELOW_NORMAL_PRIORITY_CLASS,
                        psutil.NORMAL_PRIORITY_CLASS,
                        psutil.ABOVE_NORMAL_PRIORITY_CLASS,
                        psutil.HIGH_PRIORITY_CLASS,
                        psutil.REALTIME_PRIORITY_CLASS]

    p = psutil.Process(pid if pid is not None else os.getpid())
    p.nice(priority_classes[priority])


@dataclass
class RunMkvResult:
    return_code: int = 0
    timed_out: bool = False
    stdout: list[str] = field(default_factory=list[str], repr=False)


def run_makemkvcon(message: str, args: list[str], timeout: int = 60*20) -> RunMkvResult:
    result = RunMkvResult()

    final_args = [shutil.which('makemkvcon64.exe'),
                  '--noscan', '-r', '--cache=1024', '--progress=-same']

    final_args.extend(args)
    proc = subprocess.Popen(final_args,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)

    def timeout_handler():
        nonlocal result
        result.timed_out = True
        proc.kill()

    timer = threading.Timer(timeout, timeout_handler)
    try:
        timer.start()
        while True:
            line = proc.stdout.readline().decode('utf-8').strip()
            if line:
                result.stdout.append(line)
            if not line and proc.poll() is not None:
                break
    finally:
        timer.cancel()

    result.return_code = proc.returncode

    return result