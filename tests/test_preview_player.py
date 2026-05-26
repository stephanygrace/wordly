from __future__ import annotations

import unittest

from utils.preview_target import PreviewDimensions, preview_target_dimensions


class PreviewTargetSizeTests(unittest.TestCase):
    def test_uses_frame_size_when_large_enough(self) -> None:
        self.assertEqual(
            preview_target_dimensions(PreviewDimensions(800, 450), PreviewDimensions(0, 0)),
            PreviewDimensions(800, 450),
        )

    def test_falls_back_to_host_then_default(self) -> None:
        self.assertEqual(
            preview_target_dimensions(PreviewDimensions(0, 0), PreviewDimensions(640, 360)),
            PreviewDimensions(640, 360),
        )
        self.assertEqual(
            preview_target_dimensions(PreviewDimensions(2, 2), PreviewDimensions(2, 2)),
            PreviewDimensions(640, 360),
        )


if __name__ == "__main__":
    unittest.main()
