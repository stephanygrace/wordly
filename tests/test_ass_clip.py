from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    import pysubs2
except ImportError:  # pragma: no cover
    pysubs2 = None

from utils.ass_clip import shift_ass_for_trim


@unittest.skipUnless(pysubs2 is not None, "pysubs2 not installed")
class TestAssClip(unittest.TestCase):
    def test_shift_ass_trim(self) -> None:
        subs = pysubs2.SSAFile()
        subs.events.append(pysubs2.SSAEvent(start=10_000, end=20_000, text="Hello", style="Default"))
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.ass"
            dst = Path(d) / "out.ass"
            subs.save(str(src))
            n = shift_ass_for_trim(
                source_ass=src,
                clip_start_s=12.0,
                clip_end_s=18.0,
                dest_ass=dst,
            )
            self.assertEqual(n, 1)
            out = pysubs2.load(str(dst))
            self.assertEqual(len(out.events), 1)
            self.assertGreater(out.events[0].end, out.events[0].start)
            self.assertIn("Hello", out.events[0].text)


if __name__ == "__main__":
    unittest.main()
