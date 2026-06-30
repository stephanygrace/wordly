from __future__ import annotations

import unittest

from services.downloader import (
    _download_status_message,
    _feed_subprocess_buffer,
    _progress_from_download_line,
    _progress_from_log_line,
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

    def test_cli_download_line_with_percent(self) -> None:
        update = _progress_from_download_line(
            "[download]  45.2% of ~  500.00MiB at  2.34MiB/s ETA 01:23",
        )
        self.assertIsNotNone(update)
        ratio, msg = update  # type: ignore[misc]
        self.assertAlmostEqual(ratio, 0.452)
        self.assertIn("45%", msg)
        self.assertIn("2.34MiB/s", msg)
        self.assertIn("ETA 01:23", msg)

    def test_cli_download_line_without_total(self) -> None:
        update = _progress_from_download_line(
            "[download]   1.23MiB at  456.78KiB/s",
        )
        self.assertIsNotNone(update)
        ratio, msg = update  # type: ignore[misc]
        self.assertEqual(ratio, -1.0)
        self.assertIn("1.23MiB", msg)
        self.assertIn("total size unknown", msg)

    def test_facebook_log_line(self) -> None:
        update = _progress_from_log_line(
            "[facebook] 2331486044046083: Downloading webpage",
        )
        self.assertIsNotNone(update)
        _, msg = update  # type: ignore[misc]
        self.assertIn("Facebook", msg)
        self.assertIn("Downloading webpage", msg)

    def test_carriage_return_progress_updates(self) -> None:
        seen: list[str] = []

        def capture(line: str) -> None:
            seen.append(line)

        remainder = _feed_subprocess_buffer(
            "[download]   0.0%\r[download]  12.5%\r[download]  45.2% of 500.00MiB\n",
            capture,
        )
        self.assertEqual(remainder, "")
        self.assertEqual(len(seen), 3)
        update = _progress_from_download_line(seen[-1])
        self.assertIsNotNone(update)
        ratio, msg = update  # type: ignore[misc]
        self.assertAlmostEqual(ratio, 0.452)
        self.assertIn("45%", msg)

    def test_already_downloaded_line(self) -> None:
        update = _progress_from_download_line(
            "[download] /tmp/sermon.mp4 has already been downloaded",
        )
        self.assertIsNotNone(update)
        ratio, msg = update  # type: ignore[misc]
        self.assertEqual(ratio, 1.0)
        self.assertIn("existing", msg)


if __name__ == "__main__":
    unittest.main()
