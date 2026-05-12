from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from utils.vtt_to_srt import parse_vtt_cues, vtt_file_to_srt_file


SAMPLE = """WEBVTT

00:00:01.000 --> 00:00:04.000
Hello

00:00:05.000 --> 00:00:08.000
Second
"""


class TestVttToSrt(unittest.TestCase):
    def test_parse_cues(self) -> None:
        cues = parse_vtt_cues(SAMPLE)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0][0], 1.0)
        self.assertAlmostEqual(cues[0][1], 4.0)
        self.assertIn("Hello", cues[0][2])

    def test_vtt_file_to_srt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            vtt = Path(d) / "t.vtt"
            srt = Path(d) / "o.srt"
            vtt.write_text(SAMPLE, encoding="utf-8")
            n = vtt_file_to_srt_file(vtt, srt)
            self.assertEqual(n, 2)
            text = srt.read_text(encoding="utf-8")
            self.assertIn("00:00:01,000", text)


if __name__ == "__main__":
    unittest.main()
