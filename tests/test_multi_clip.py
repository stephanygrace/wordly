from __future__ import annotations

import unittest

from services.multi_clip import _format_clip_progress


class TestMultiClipProgress(unittest.TestCase):
    def test_format_clip_progress(self) -> None:
        overall, text = _format_clip_progress(2, 4, 0.5, "Copying highlight")
        self.assertAlmostEqual(overall, 0.375)
        self.assertIn("38%", text)
        self.assertIn("[2/4]", text)
        self.assertIn("Copying highlight", text)

    def test_format_clip_progress_first_clip_start(self) -> None:
        overall, text = _format_clip_progress(1, 3, 0.0, "Preparing")
        self.assertAlmostEqual(overall, 0.0)
        self.assertIn("0%", text)


if __name__ == "__main__":
    unittest.main()
