import logging
from pathlib import Path


class VideoManager:
    def __init__(self, data_folder: str | None, force: bool, input_filter: str):
        self.data_folder: Path = data_folder if data_folder is None else Path(data_folder)
        self.force: bool = force
        self.input_filter: str = input_filter
        self.log_level = logging.DEBUG
        self.minimum_title_len = 60*3