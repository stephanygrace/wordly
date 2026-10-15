from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from services.downloader import (
    _download_status_message,
    _download_with_executable,
    _existing_download_for_url,
    _feed_subprocess_buffer,
    _progress_from_download_line,
    _progress_from_log_line,
    _progress_from_postprocessor_hook,
    _progress_from_ytdlp_hook,
    _source_label,
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

    def test_existing_download_for_facebook_url(self) -> None:
        with TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            expected = out_dir / "Sermon [3031597830564656].mp4"
            expected.write_bytes(b"video")

            found = _existing_download_for_url(
                "https://www.facebook.com/watch/?v=3031597830564656",
                out_dir,
            )

        self.assertEqual(found, expected.resolve())

    def test_existing_download_ignores_non_video_matches(self) -> None:
        with TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "Sermon [3031597830564656].json").write_text("{}", encoding="utf-8")

            found = _existing_download_for_url(
                "https://www.facebook.com/watch/?v=3031597830564656",
                out_dir,
            )

        self.assertIsNone(found)


class TestExecutableDownloadProgress(unittest.TestCase):
    def _run(self, output_lines: list[str], out_file: Path) -> tuple[list[str], list[tuple[float, str]]]:
        seen: list[tuple[float, str]] = []
        captured: dict = {}

        def fake_run(cmd, *, should_cancel, on_line, **_kwargs):
            captured["cmd"] = cmd
            for line in output_lines:
                on_line(line)
            on_line(str(out_file))
            return 0

        with mock.patch("services.downloader._run_subprocess_with_lines", side_effect=fake_run):
            _download_with_executable(
                "yt-dlp",
                "https://www.youtube.com/watch?v=abc",
                template="%(id)s.%(ext)s",
                progress_cb=lambda r, m: seen.append((r, m)),
                should_cancel=None,
                cookies_file=None,
            )
        return captured["cmd"], seen

    def test_forces_progress_output_despite_print(self) -> None:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "v.mp4"
            out.write_bytes(b"x")
            cmd, _ = self._run([], out)
        self.assertIn("--print", cmd)
        self.assertIn("--progress", cmd)

    def test_labels_second_stream_pass(self) -> None:
        lines = [
            "[download]  50.0% of  10.00MiB at  1.00MiB/s ETA 00:05",
            "[download] 100% of  10.00MiB in 00:00:10 at 1.00MiB/s",
            "[download]  25.0% of  2.00MiB at  1.00MiB/s ETA 00:01",
        ]
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "v.mp4"
            out.write_bytes(b"x")
            _, seen = self._run(lines, out)
        self.assertEqual(seen[0], (0.5, "Downloading — 50% (10.00MiB) · 1.00MiB/s · ETA 00:05"))
        self.assertAlmostEqual(seen[-1][0], 0.25)
        self.assertTrue(seen[-1][1].startswith("Downloading (part 2) — 25%"))

    def test_source_label(self) -> None:
        self.assertEqual(_source_label("https://youtu.be/abc"), "YouTube")
        self.assertEqual(_source_label("https://www.facebook.com/watch/?v=1"), "Facebook")
        self.assertEqual(_source_label("https://vimeo.com/1"), "the link")


if __name__ == "__main__":
    unittest.main()
