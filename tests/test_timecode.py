from __future__ import annotations

import unittest

from utils.timecode import (
    end_timecode_from_start_offset,
    format_timecode_digits,
    normalize_four_digit_timecode,
    parse_timecode,
    validate_range,
    validate_segment_times,
)


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

    def test_format_timecode_digits(self) -> None:
        self.assertEqual(format_timecode_digits("01"), "01")
        self.assertEqual(format_timecode_digits("0125"), "01:25")
        self.assertEqual(format_timecode_digits("012530"), "01:25:30")
        self.assertEqual(format_timecode_digits("01:25:30"), "01:25:30")
        self.assertEqual(format_timecode_digits("01253099"), "01:25:30")

    def test_normalize_four_digit_timecode(self) -> None:
        self.assertEqual(normalize_four_digit_timecode("01:45"), "01:45:00")
        self.assertEqual(normalize_four_digit_timecode("01:45:00"), "01:45:00")
        self.assertEqual(normalize_four_digit_timecode("01:45:30"), "01:45:30")
        self.assertEqual(
            end_timecode_from_start_offset(normalize_four_digit_timecode("01:45"), 30),
            "01:45:30",
        )
        self.assertEqual(
            end_timecode_from_start_offset(normalize_four_digit_timecode("01:45"), 60),
            "01:46:00",
        )

    def test_end_timecode_from_start_offset(self) -> None:
        self.assertEqual(
            end_timecode_from_start_offset("00:10:00", 30),
            "00:10:30",
        )
        self.assertEqual(
            end_timecode_from_start_offset("00:10:00", 60, media_duration_s=3600),
            "00:11:00",
        )
        self.assertEqual(
            end_timecode_from_start_offset("00:59:45", 30, media_duration_s=3600),
            "01:00:00",
        )
        clamped = end_timecode_from_start_offset("00:59:45", 60, media_duration_s=3600)
        self.assertEqual(clamped, "01:00:00")


if __name__ == "__main__":
    unittest.main()
