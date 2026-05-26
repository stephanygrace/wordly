from __future__ import annotations

import unittest

from services.downloader import (
    _download_status_message,
    _progress_from_postprocessor_hook,
    _progress_from_ytdlp_hook,
)


class TestDownloaderProgress(unittest.TestCase):
    def test_byte_progress_message(self) -> None:
        d = {
            "status": "downloading",
            "downloaded_bytes": 5 * 1024 * 1024,
            "total_bytes": 10 * 1024 * 1024,
            "speed": 2 * 1024 * 1024,
            "eta": 3,
        }
        ratio, msg = _progress_from_ytdlp_hook(d)  # type: ignore[misc]
        self.assertAlmostEqual(ratio, 0.5)
        self.assertIn("50%", msg)
        self.assertIn("5.0 MiB / 10.0 MiB", msg)
        self.assertIn("2.0 MiB/s", msg)
        self.assertIn("ETA 0:03", msg)

    def test_fragment_progress_message(self) -> None:
        d = {
            "status": "downloading",
            "fragment_index": 5,
            "fragment_count": 10,
            "downloaded_bytes": 1024,
        }
        ratio, msg = _progress_from_ytdlp_hook(d)  # type: ignore[misc]
        self.assertAlmostEqual(ratio, 0.5)
        self.assertIn("50%", msg)
        self.assertIn("fragment 5/10", msg)

    def test_unknown_total_shows_downloaded_bytes(self) -> None:
        msg = _download_status_message(
            {"downloaded_bytes": 2048, "speed": 1024},
            -1.0,
        )
        self.assertIn("2.0 KiB", msg)
        self.assertIn("total size unknown", msg)

    def test_finished_stream_message(self) -> None:
        update = _progress_from_ytdlp_hook(
            {"status": "finished", "filename": "/tmp/sermon.f140.mp4"},
        )
        self.assertIsNotNone(update)
        ratio, msg = update  # type: ignore[misc]
        self.assertEqual(ratio, -1.0)
        self.assertIn("sermon.f140.mp4", msg)

    def test_postprocessor_hook(self) -> None:
        update = _progress_from_postprocessor_hook(
            {"status": "started", "postprocessor": "Merger"},
        )
        self.assertIsNotNone(update)
        _, msg = update  # type: ignore[misc]
        self.assertIn("Merger", msg)


if __name__ == "__main__":
    unittest.main()
