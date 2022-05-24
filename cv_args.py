from dataclasses import dataclass
from pathlib import Path


@dataclass(kw_only=True)
class ConvertVideoArgs:
    input_folder: Path
    force: bool
    min_title_length: int
    file_filter: str
    temp_folder: Path
    log_level: str
    log_file: Path
    database_folder: Path
    subcommand_name: str = None
    timeout: int = None