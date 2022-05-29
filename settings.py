import logging
from pathlib import Path
from discinfo import DiscInfo
import pickle
import fnmatch


class VideoManager:
    _pkl_name = 'discs.pkl'

    def __init__(self, data_folder: str | None, force: bool, input_filter: str):
        self.data_folder: Path = data_folder if data_folder is None else Path(data_folder)
        self.force: bool = force
        self.input_filter: str = input_filter
        self.log_level = logging.DEBUG
        self.minimum_title_len = 60*3

    def info_dict(self) -> dict[str, DiscInfo]:
        result: dict[str, DiscInfo] = {}
        if self.data_folder is not None:
            try:
                with Path(self.data_folder, self._pkl_name).open('rb') as file:
                    result = pickle.load(file)
            except IOError:
                pass

        return result

    def save_info_dict(self, info_dict: dict[str, DiscInfo]) -> None:
        if self.data_folder is not None:
            with Path(self.data_folder, self._pkl_name).open('wb') as file:
                pickle.dump(info_dict, file)

    def scan_for_video_files(self, input_folder: Path) -> list[Path]:
        return [F for F in input_folder.rglob('*.iso') if
                self.input_filter is None or fnmatch.fnmatch(str(F), self.input_filter)]