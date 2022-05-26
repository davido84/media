import logging
from dataclasses import dataclass
from pathlib import Path


@dataclass(kw_only=True)
class ConvertVideoArgs:
    input_folder: Path
    force: bool
    filename_filter: str
    log_level: str
    database_folder: Path
    yaml: bool
    log: bool
    command: str
    timeout: int = None
    temp_folder: Path = None
    output_folder: Path = None
    min_title_length: int = None
    validate_status: str = None


class VideoManager:
    def __init__(self, data_folder: str, log: bool, force: bool, input_filter: str):
        self.data_folder: Path = Path(data_folder)
        self.log: bool = log
        self.force: bool = force
        self.input_filter: str = input_filter
        self.log_level = logging.DEBUG