from settings import VideoManager
import logging
from dataclasses import dataclass, field
from pprint import pformat
from mediautil import gigabyte_string, run_makemkvcon
import subprocess

logger = logging.getLogger('SCAN')


@dataclass
class Result:
    num_successful: int = 0
    num_corrupt: int = 0
    num_timeouts: int = 0
    max_title_size: int = field(repr=False, default=0)


def command(settings: VideoManager, timeout: int) -> int:
    logging.info(f'Timeout={timeout}')
    run_makemkvcon('The message', ['mkv', f'iso:e:/movies/test-g.iso', '0', 'e:/movies'])
    result = Result()
    logger.info('%s', pformat(result))
    logger.info(f'Max title size: {gigabyte_string(result.max_title_size)}')
    return 0