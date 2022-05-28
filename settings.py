import logging
from dataclasses import dataclass
from pathlib import Path
import os


class VideoManager:
    def __init__(self, data_folder: str, force: bool, input_filter: str):
        self.data_folder: Path = Path(data_folder)
        self.force: bool = force
        self.input_filter: str = input_filter
        self.log_level = logging.DEBUG
        self.minimum_title_len = 60*3