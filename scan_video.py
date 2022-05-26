from settings import VideoManager
import logging
from dataclasses import dataclass

logger = logging.getLogger('SCAN')


@dataclass
class Result:
    num_corrupt: int = 0
    num_timeouts: int = 0
    num_successful: int = 0


def command(settings: VideoManager, timeout: int) -> Result:
    result = Result()
    return result

    # logger.debug('About to start')
    # logger.info(f'Starting {__name__}')