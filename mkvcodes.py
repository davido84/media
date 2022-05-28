from enum import IntEnum
from dataclasses import dataclass


@dataclass
class MkvInfo:
    code: int
    message_code: int
    value: str