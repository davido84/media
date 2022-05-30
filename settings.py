import logging
from pathlib import Path
from isodisc import IsoDisc
import pickle
import fnmatch
import pyaml
from dataclasses import asdict


class VideoManager:
    _pkl_name = 'discs.pkl'
    _yaml_name = 'discs.yaml'

    def __init__(self, data_folder: str | None, force: bool, input_filter: str):
        self.data_folder: Path = data_folder if data_folder is None else Path(data_folder)
        self.force: bool = force
        self.input_filter: str = input_filter
        self.log_level = logging.DEBUG
        self.minimum_title_len = 60*3

    def iso_dict(self) -> dict[str, IsoDisc]:
        result: dict[str, IsoDisc] = {}
        if self.data_folder is not None:
            try:
                with Path(self.data_folder, self._pkl_name).open('rb') as file:
                    result = pickle.load(file)
            except IOError:
                pass

        return result

    def save_iso_dict(self, info_dict: dict[str, IsoDisc]) -> None:
        if self.data_folder is not None:
            with Path(self.data_folder, self._pkl_name).open('wb') as file:
                pickle.dump(info_dict, file)

    def save_yaml_dict(self, info_dict: dict[str, IsoDisc]) -> None:
        if self.data_folder is not None:
            yaml_dict = {}
            for key, value in info_dict.items():
                yaml_dict[key] = asdict(value)
            Path(self.data_folder, self._yaml_name).write_text(pyaml.dump(yaml_dict, sort_dicts=False))

    def scan_for_video_files(self, input_folder: Path) -> list[Path]:
        return [F for F in input_folder.rglob('*.iso') if
                self.input_filter is None or fnmatch.fnmatch(str(F), self.input_filter)]