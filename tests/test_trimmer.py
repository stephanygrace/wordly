from __future__ import annotations

import unittest

from services.trimmer import clamp_trim_to_duration, parse_trim_times


class TestTrimmer(unittest.TestCase):
    def test_parse_trim(self) -> None:
        spec = parse_trim_times("00:01:00", "00:01:30")
        self.assertAlmostEqual(spec.start_seconds, 60.0)
        self.assertAlmostEqual(spec.end_seconds, 90.0)

    def test_clamp(self) -> None:
        spec = parse_trim_times("00:00:00", "01:00:00")
        c = clamp_trim_to_duration(spec, 120.0)
        self.assertLessEqual(c.end_seconds, 120.0)
        self.assertLess(c.start_seconds, c.end_seconds)


if __name__ == "__main__":
    unittest.main()
