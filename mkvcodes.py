from enum import IntEnum


class DiscType(IntEnum):
    DVD = 6206
    BD = 6209
    MKV = 6213


class StreamType(IntEnum):
    VIDEO = 6201
    AUDIO = 6202
    SUBTITLE = 6203


class MkvStatus(IntEnum):
    CORRUPT: int = 4004
    SUCCESS: int = 5011
    FAILED: int = 0
    FAILED_TO_OPEN_DISC: int = 5010


INFO_KEY_CODE: int = 1


KEY_CODES_DICT: dict[int, tuple[str, type]] = {
    INFO_KEY_CODE: ('info', str),
    2: ('name', str),
    3: ('language_code', str),
    4: ('language', str),
    5: ('codec', str),
    8: ('chapter_count', int),
    9: ('length', str),
    10: ('size_human', str),
    11: ('size', int),
    15: ('video_angle', str),
    17: ('sample_rate', int),
    19: ('screen_size', str),
    20: ('aspect_ratio', str),
    21: ('frame_rate', str),
    24: ('source_title_id', str),
    25: ('segment_count', str),
    26: ('segment_map', str),
    27: ('file_output', str),
    28: ('language_code', str),
    29: ('language', str),
    30: ('information', str),
    39: ('default', bool),
}