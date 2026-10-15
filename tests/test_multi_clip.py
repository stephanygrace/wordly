from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from services.multi_clip import (
    _format_clip_progress,
    _prefer_reencode_for_editing,
    _source_video_codec,
)


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

    def test_source_video_codec_reads_ffprobe_json(self) -> None:
        with patch("services.multi_clip.find_ffprobe", return_value="/usr/bin/ffprobe"):
            with patch(
                "services.multi_clip.subprocess.check_output",
                return_value='{"streams":[{"codec_name":"vp9"}]}',
            ):
                codec = _source_video_codec(Path("/tmp/video.mp4"))
        self.assertEqual(codec, "vp9")

    def test_prefer_reencode_for_editing_for_vp9(self) -> None:
        with patch("services.multi_clip._source_video_codec", return_value="vp9"):
            self.assertTrue(_prefer_reencode_for_editing(Path("/tmp/video.mp4")))

    def test_prefer_reencode_for_editing_for_h264(self) -> None:
        with patch("services.multi_clip._source_video_codec", return_value="h264"):
            self.assertFalse(_prefer_reencode_for_editing(Path("/tmp/video.mp4")))


if __name__ == "__main__":
    unittest.main()
