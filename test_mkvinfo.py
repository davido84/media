from unittest import TestCase
from mkv_test_data import GOOD_ENG_BD, GOOD_ENG_DVD, BAD_ENG_DVD
from mkvcodes import MkvStatus, DiscType


class TestDiscInfo(TestCase):
    def setUp(self) -> None:
        pass
        # self.good_dvd: DiscInfo = DiscInfo(GOOD_ENG_DVD)
        # self.bad_dvd: DiscInfo = DiscInfo(BAD_ENG_DVD)
        # self.good_bd: DiscInfo = DiscInfo(GOOD_ENG_BD)

    # def test_disc_type(self):
    #     self.assertEqual(self.good_dvd.disc_type(), DiscType.DVD)
    #     self.assertEqual(self.bad_dvd.disc_type(), DiscType.DVD)
    #     self.assertEqual(self.good_bd.disc_type(), DiscType.BD)
    #
    # def test_validate(self):
    #     self.assertEqual(self.good_bd.validate(), DiscInfo.ValidationCode.OK)
    #     self.assertEqual(self.good_dvd.validate(), DiscInfo.ValidationCode.OK)
    #     self.assertEqual(self.bad_dvd.validate(), DiscInfo.ValidationCode.POSSIBLE_ERROR)

    def test_titles(self):
        pass