from settings import VideoManager
import sys
import logging
import os
import scan_video
import validate_video
import transcode_mkv
import extract_mkv
import click
from pathlib import Path
from mediautil import media_command

pass_vm = click.make_pass_decorator(VideoManager)


@click.group(no_args_is_help=True)
@click.option(
    '--data-folder',
    envvar='CV_DATA_FOLDER',
    default='d:/movies',
    metavar='PATH',
    help='Changes the data folder')
@click.option(
    '--force/--no-force', '-f',
    envvar='CV_FORCE',
    default=False,
    help='Process all files.')
@click.option(
    '--filename-filter', '-r',
    metavar='FILENAME FILTER',
    help='Filename filter. Example: "*d1*.*'
)
@click.version_option("1.0")
@click.pass_context
def cli(ctx, data_folder: str, force: bool, filename_filter: str) -> int:
    """Video Manager"""
    os.makedirs(data_folder, exist_ok=True)
    root_logger = logging.getLogger()
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%m/%d %I:%M %p',
                        stream=sys.stdout)
    file_formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='%m/%d %I:%M %p')
    log_file_name = Path(data_folder, 'cv.log')
    file_handler = logging.FileHandler(encoding='utf-8', filename=f'{log_file_name}', mode='w')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)
    ctx.obj = VideoManager(data_folder, force, filename_filter)
    return 0


@cli.command()
@click.option('--timeout', '-t', help='Timeout in seconds', type=click.INT, default=4*60)
@click.argument('input_folder', metavar='INPUT_FOLDER')
@pass_vm
def scan(vm: VideoManager, timeout: int, input_folder: str):
    """Rebuild database from ISO/MKV files.
        Files which already exist in the database are skipped unless --force is specified.
    """
    with media_command('SCAN'):
        scan_video.command(vm, timeout)


@cli.command()
@pass_vm
def validate(vm: VideoManager):
    """Validate disc ISO and MKV files from database.
        Files which are already validated are skipped unless --force is specified
    """
    validate_video.command(vm)


@cli.command()
@pass_vm
def transcode(vm: VideoManager):
    """Transcode MKV files to create library."""
    transcode_mkv.command(vm)


@cli.command()
@pass_vm
def extract(vm: VideoManager):
    """Extract all mkv files from ISO files."""
    extract_mkv.command(vm)


if __name__ == "__main__":
    sys.exit(cli())