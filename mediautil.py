import os
import threading
import subprocess
import shutil
import logging

class RunMkvConResult:
    return_code: int = 0
    timed_out: bool = False
    stdout: list[str] = []


def run_mkvcon(title: str, args: list[str]) ->RunMkvConResult:
    final_args = [shutil.which('makemkvcon64.exe'), '--noscan', '-r', '--cache=2048']
    final_args.extend(args)

    proc = subprocess.Popen(final_args,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT)

    result = RunMkvConResult()

    if proc.returncode == 0:
        while True:
            try:
                line = proc.stdout.readline().decode('utf-8').strip()
            except UnicodeDecodeError:
                logging.warning('UnicodeDecodeError')
                continue

            result.stdout.append(line)
            if not line and proc.poll() is not None:
                break

    result.return_code = proc.returncode
    return RunMkvConResult()