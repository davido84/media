from settings import VideoManager
import sys
import logging
import os
from mediautil import Timer
import scan_video
import validate_video
import transcode_mkv
import extract_mkv
import click

pass_vm = click.make_pass_decorator(VideoManager)


@click.group(no_args_is_help=True)
@click.option(
    '--data-folder',
    envvar='CV_DATA_FOLDER',
    default='.data',
    metavar='PATH',
    help='Changes the data folder')
@click.option(
    '--log/--no-log',
    envvar='CV_LOG',
    default=True,
    help='Output log file')
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
def cli(ctx, data_folder: str, log: bool, force: bool, filename_filter: str) -> int:
    """Video Manager"""
    ctx.obj = VideoManager(data_folder, log, force, filename_filter)
    return 0


@cli.command()
@click.option('--timeout', '-t', help='Timeout in seconds', type=click.INT, default=4*60)
@pass_vm
def scan(vm: VideoManager, timeout: int):
    """Rebuild database from ISO/MKV files.
        Files which already exist in the database are skipped unless --force is specified.
    """
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