import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.utils.time import get_kyiv_reset_time


class TestTimeUtils(unittest.TestCase):
    def setUp(self):
        # We need the real datetime class for combine and min
        self.real_datetime = datetime
        # Use real zoneinfo timezones directly (avoids any mock contamination)
        self.pacific_tz = ZoneInfo("US/Pacific")

    @patch("app.utils.time.datetime")
    def test_get_kyiv_reset_time_standard(self, mock_datetime):
        """Test reset time calculation during standard time difference (10 hours)."""
        fixed_now = self.real_datetime(2023, 10, 26, 10, 0, 0, tzinfo=self.pacific_tz)

        # Properly route all other datetime methods to the real class
        mock_datetime.now.side_effect = lambda tz=None: fixed_now
        mock_datetime.combine.side_effect = self.real_datetime.combine
        mock_datetime.min = self.real_datetime.min
        mock_datetime.side_effect = lambda *args, **kw: self.real_datetime(*args, **kw)

        # Call the function
        result = get_kyiv_reset_time()

        # Assert: 10:00 27.10.2023
        self.assertEqual(result, "10:00 27.10.2023")

    @patch("app.utils.time.datetime")
    def test_get_kyiv_reset_time_dst_mismatch(self, mock_datetime):
        """Test reset time calculation during mismatched DST (late October, 9 hours diff)."""
        fixed_now = self.real_datetime(2023, 10, 30, 12, 0, 0, tzinfo=self.pacific_tz)

        mock_datetime.now.side_effect = lambda tz=None: fixed_now
        mock_datetime.combine.side_effect = self.real_datetime.combine
        mock_datetime.min = self.real_datetime.min
        mock_datetime.side_effect = lambda *args, **kw: self.real_datetime(*args, **kw)

        # Call
        result = get_kyiv_reset_time()

        # Assert: 09:00 31.10.2023
        self.assertEqual(result, "09:00 31.10.2023")

    @patch("app.utils.time.datetime")
    def test_get_kyiv_reset_time_year_rollover(self, mock_datetime):
        """Test reset time calculation across year boundary."""
        fixed_now = self.real_datetime(2023, 12, 31, 23, 0, 0, tzinfo=self.pacific_tz)

        mock_datetime.now.side_effect = lambda tz=None: fixed_now
        mock_datetime.combine.side_effect = self.real_datetime.combine
        mock_datetime.min = self.real_datetime.min
        mock_datetime.side_effect = lambda *args, **kw: self.real_datetime(*args, **kw)

        # Call
        result = get_kyiv_reset_time()

        # Assert: 10:00 01.01.2024
        self.assertEqual(result, "10:00 01.01.2024")

        # Call
        result = get_kyiv_reset_time()

        # Assert: 10:00 01.01.2024
        self.assertEqual(result, "10:00 01.01.2024")


if __name__ == "__main__":
    unittest.main()
