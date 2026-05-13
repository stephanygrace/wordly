from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.download_backend import (
    import_video_to_folder,
    snapshot_watch_dirs,
    wait_for_idm_video,
)


class TestDownloadBackend(unittest.TestCase):
    def test_import_video_moves_into_wordly_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = root / "external"
            downloads = root / "downloads"
            external.mkdir()
            downloads.mkdir()
            video = external / "sermon.mp4"
            video.write_bytes(b"\x00" * 64_000)
            imported = import_video_to_folder(video, downloads)
            self.assertEqual(imported.parent, downloads.resolve())
            self.assertTrue(imported.is_file())
            self.assertFalse(video.exists())

    def test_wait_for_idm_video_detects_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            downloads.mkdir()
            before = snapshot_watch_dirs([downloads])

            def add_file() -> None:
                (downloads / "new_sermon.mp4").write_bytes(b"\x00" * 64_000)

            import threading

            threading.Timer(0.2, add_file).start()
            path = wait_for_idm_video(
                downloads,
                [downloads],
                before,
                timeout_s=5.0,
            )
            self.assertEqual(path.name, "new_sermon.mp4")
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
