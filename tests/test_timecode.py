from __future__ import annotations

import unittest

from utils.timecode import parse_timecode, validate_range, validate_segment_times


class TestTimecode(unittest.TestCase):
    def test_parse_hms(self) -> None:
        self.assertAlmostEqual(parse_timecode("01:02:03").total_seconds, 3723.0)

    def test_parse_ms(self) -> None:
        self.assertAlmostEqual(parse_timecode("02:30").total_seconds, 150.0)

    def test_validate_range_rejects_equal(self) -> None:
        with self.assertRaises(ValueError):
            validate_range(10.0, 10.0)

    def test_validate_segment_times(self) -> None:
        start, end = validate_segment_times("00:01:00", "00:02:00", media_duration_s=3600.0)
        self.assertAlmostEqual(start, 60.0)
        self.assertAlmostEqual(end, 120.0)

    def test_validate_segment_times_rejects_past_duration(self) -> None:
        with self.assertRaises(ValueError):
            validate_segment_times("01:00:00", "01:05:00", media_duration_s=3000.0)


if __name__ == "__main__":
    unittest.main()
