from __future__ import annotations

import unittest

from services.trimmer import (
    _duration_from_ffmpeg_stderr,
    _ffmpeg_probe_stderr,
    clamp_trim_to_duration,
    parse_trim_times,
)


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

    def test_ffmpeg_probe_does_not_decode_entire_file(self) -> None:
        import inspect

        source = inspect.getsource(_ffmpeg_probe_stderr)
        self.assertNotIn('"null"', source)
        self.assertNotIn("'-f'", source)

    def test_duration_from_ffmpeg_stderr(self) -> None:
        stderr = (
            "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'sermon.mp4':\n"
            "  Duration: 02:33:06.26, start: 0.000000, bitrate: 3578 kb/s\n"
        )
        self.assertAlmostEqual(_duration_from_ffmpeg_stderr(stderr), 9186.26, places=1)


if __name__ == "__main__":
    unittest.main()
