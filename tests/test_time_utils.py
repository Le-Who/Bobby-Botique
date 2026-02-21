import unittest
from unittest.mock import patch
from datetime import datetime
from app.utils.time import get_kyiv_reset_time, get_pacific_tz


class TestTimeUtils(unittest.TestCase):
    def setUp(self):
        # We need the real datetime class for combine and min
        self.real_datetime = datetime

    @patch("app.utils.time.datetime")
    def test_get_kyiv_reset_time_standard(self, mock_datetime):
        """Test reset time calculation during standard time difference (10 hours)."""
        # Configure mock to behave like datetime class where needed
        mock_datetime.combine = self.real_datetime.combine
        mock_datetime.min = self.real_datetime.min

        # Define a fixed time in Pacific Time
        # October 26, 2023 10:00:00 PDT (UTC-7)
        # Next day is Oct 27, 2023. Reset at 00:00 PDT = 07:00 UTC.
        # Kyiv is UTC+3 (EEST). 07:00 UTC + 3 = 10:00 Kyiv.
        pacific_tz = get_pacific_tz()
        # Create a timezone-aware datetime
        fixed_now = self.real_datetime(2023, 10, 26, 10, 0, 0, tzinfo=pacific_tz)
        mock_datetime.now.return_value = fixed_now

        # Call the function
        result = get_kyiv_reset_time()

        # Assert: 10:00 27.10.2023
        self.assertEqual(result, "10:00 27.10.2023")

    @patch("app.utils.time.datetime")
    def test_get_kyiv_reset_time_dst_mismatch(self, mock_datetime):
        """Test reset time calculation during mismatched DST (late October, 9 hours diff)."""
        # Europe switches back to standard time last Sunday of Oct (Oct 29, 2023).
        # US switches back first Sunday of Nov (Nov 5, 2023).
        # So on Oct 30, US is PDT (UTC-7) and Kyiv is EET (UTC+2).
        # Diff is 2 - (-7) = 9 hours.

        # Configure mock
        mock_datetime.combine = self.real_datetime.combine
        mock_datetime.min = self.real_datetime.min

        # Fixed time: Oct 30, 2023 12:00:00 PDT
        # Next day: Oct 31. Reset at 00:00 PDT = 07:00 UTC.
        # Kyiv (UTC+2): 07:00 UTC + 2 = 09:00 Kyiv.
        pacific_tz = get_pacific_tz()
        fixed_now = self.real_datetime(2023, 10, 30, 12, 0, 0, tzinfo=pacific_tz)
        mock_datetime.now.return_value = fixed_now

        # Call
        result = get_kyiv_reset_time()

        # Assert: 09:00 31.10.2023
        self.assertEqual(result, "09:00 31.10.2023")

    @patch("app.utils.time.datetime")
    def test_get_kyiv_reset_time_year_rollover(self, mock_datetime):
        """Test reset time calculation across year boundary."""
        # Dec 31, 2023.
        # Next day Jan 1, 2024.

        # Configure mock
        mock_datetime.combine = self.real_datetime.combine
        mock_datetime.min = self.real_datetime.min

        # Fixed time: Dec 31, 2023 23:00:00 PST (UTC-8)
        # Next day: Jan 1. Reset at 00:00 PST = 08:00 UTC.
        # Kyiv (UTC+2): 08:00 UTC + 2 = 10:00 Kyiv. (Diff 10h)
        pacific_tz = get_pacific_tz()
        fixed_now = self.real_datetime(2023, 12, 31, 23, 0, 0, tzinfo=pacific_tz)
        mock_datetime.now.return_value = fixed_now

        # Call
        result = get_kyiv_reset_time()

        # Assert: 10:00 01.01.2024
        self.assertEqual(result, "10:00 01.01.2024")


if __name__ == "__main__":
    unittest.main()
