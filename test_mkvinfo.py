from unittest import TestCase
from mkv_test_data import GOOD_ENG_BD, GOOD_ENG_DVD, BAD_ENG_DVD
from discinfo import parse_disc, DiscInfo


class TestDiscInfo(TestCase):
    def setUp(self) -> None:
        self.good_dvd: DiscInfo = parse_disc(GOOD_ENG_DVD.split())
        self.bad_dvd: DiscInfo = parse_disc(BAD_ENG_DVD.split())
        self.good_bd: DiscInfo = parse_disc(GOOD_ENG_BD.split())

    def test_validate(self):
        self.assertFalse(self.good_bd.is_corrupt)
        self.assertFalse(self.good_dvd.is_corrupt)
        self.assertTrue(self.bad_dvd.is_corrupt)