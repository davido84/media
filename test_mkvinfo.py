from unittest import TestCase
from mkv_test_data import GOOD_ENG_BD, GOOD_ENG_DVD, BAD_ENG_DVD
from isodisc import parse_disc, IsoDisc


class TestDiscInfo(TestCase):
    def setUp(self) -> None:
        self.good_dvd: IsoDisc = parse_disc(GOOD_ENG_DVD.split())
        self.bad_dvd: IsoDisc = parse_disc(BAD_ENG_DVD.split())
        self.good_bd: IsoDisc = parse_disc(GOOD_ENG_BD.split())

    def test_validate(self):
        self.assertFalse(self.good_bd.problematic)
        self.assertFalse(self.good_dvd.problematic)
        self.assertTrue(self.bad_dvd.problematic)
