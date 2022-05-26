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


class Timer:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.timer = default_timer

    def __enter__(self):
        self.start = self.timer()
        return self

    def __exit__(self, *args):
        end = self.timer()
        self.elapsed_secs = (end - self.start)
        if self.verbose:
            print(f'Elapsed time: {self.format_delta()}')

    def format_delta(self) -> str:
        return str(timedelta(seconds=math.floor(self.elapsed_secs)))


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


def run_makemkvcon(args: list[str], timeout: int = 10*60) -> str:
    proc = subprocess.Popen([shutil.which('makemkvcon64.exe')],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)

    output: list[str] = []

    def timeout_handler():
        proc.kill()
        raise subprocess.TimeoutExpired

    timer = threading.Timer(timeout, timeout_handler)
    try:
        while True:
            line = str(proc.stdout.readline())
            output.append(line)
            print(line)
            if not line and proc.poll() is not None:
                break
    finally:
        timer.cancel()

    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, '')

    return ''.join(output)