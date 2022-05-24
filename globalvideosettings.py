from dataclasses import dataclass, field, InitVar
from pathlib import Path
import os
from cv import DiscInfo
import pickle
from contextlib import contextmanager
import yaml
import logging
import logging.handlers


def _setup_logger(logger: logging.Logger, log_file: Path):
    def custom_name(default_name):
        return default_name + '.log'

    logger.setLevel(logging.DEBUG)
    logging.basicConfig(encoding='utf-8',  # filename=Path(self.media_config_folder, 'video.log'),
                        format='%(asctime)s %(levelname)s: %(message)s', datefmt='%m/%d %I:%M %p')

    handler = logging.handlers.RotatingFileHandler(str(log_file), maxBytes=64, backupCount=5)

    handler.namer = custom_name

    logger.addHandler(handler)
    logger.propagate = False


@dataclass
class GlobalSettings:
    logger_name: InitVar[str]
    config_folder_init: InitVar[Path]
    config_folder: Path = field(default=False, init=False)
    logger: logging.Logger = field(default=False, init=False)

    def __post_init__(self, logger_name: str, config_folder_init: Path):
        self.config_folder = config_folder_init
        self.logger = logging.getLogger(logger_name)
        os.makedirs(self.config_folder, exist_ok=True)
        _setup_logger(self.logger, Path(self.config_folder, logger_name))


@dataclass
class GlobalVideoSettings(GlobalSettings(logger_name='video.log',
                                         config_folder_init=Path(os.environ['MEDIA_DATA_FOLDER']))):
    mkv_minlength: int = 60*3
    # media_config_folder: Path = Path(os.environ['MEDIA_DATA_FOLDER'])
    # log_filename: str = 'video.log'
    # logger: logging.Logger = logging.getLogger('media')

    # def __post_init__(self):
    #     assert self.media_config_folder
    #     os.makedirs(str(self.media_config_folder), exist_ok=True)
    #     _setup_logger(self.logger, Path(self.media_config_folder, 'video.log'))

    @contextmanager
    def disc_info_dict(self, clear: bool = False):
        config_folder = self.config_folder
        disc_info_file = Path(config_folder, 'disc-info.pkl')
        yaml_info_file = Path(config_folder, 'disc-info.yaml')
        disc_info: dict[Path, DiscInfo] = pickle.load(disc_info_file.open('rb')
                                                      ) if disc_info_file.exists() and not clear else {}
        try:
            yield disc_info
        finally:
            pickle.dump(disc_info, disc_info_file.open('wb'))
            yaml_info_file.write_text(yaml.dump(disc_info))