from __future__ import annotations

import unittest

from utils.ytdlp_paths import parse_yt_dlp_version


class TestYtDlpPaths(unittest.TestCase):
    def test_parse_version(self) -> None:
        self.assertEqual(parse_yt_dlp_version("2026.06.09"), (2026, 6, 9))
        self.assertEqual(parse_yt_dlp_version("yt-dlp 2025.10.14\n"), (2025, 10, 14))
        self.assertIsNone(parse_yt_dlp_version("n/a"))


if __name__ == "__main__":
    unittest.main()
