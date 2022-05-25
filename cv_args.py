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