from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from utils.srt_clip import shift_srt_for_trim


class TestSrtClip(unittest.TestCase):
    def test_shift_trim_keeps_overlapping_cue(self) -> None:
        srt = (
            "1\n"
            "00:00:10,000 --> 00:00:20,000\n"
            "Hello\n"
            "\n"
            "2\n"
            "00:01:00,000 --> 00:01:05,000\n"
            "Outside\n"
        )
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.srt"
            dst = Path(d) / "out.srt"
            src.write_text(srt, encoding="utf-8")
            n = shift_srt_for_trim(
                source_srt=src,
                clip_start_s=12.0,
                clip_end_s=18.0,
                dest_srt=dst,
            )
            self.assertEqual(n, 1)
            body = dst.read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:06,000", body)
            self.assertIn("Hello", body)

    def test_shift_trim_empty_window(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.srt"
            dst = Path(d) / "out.srt"
            src.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nX\n",
                encoding="utf-8",
            )
            n = shift_srt_for_trim(
                source_srt=src,
                clip_start_s=5.0,
                clip_end_s=6.0,
                dest_srt=dst,
            )
            self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
