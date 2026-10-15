from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from services import music_downloader as md


class TestLibrary(unittest.TestCase):
    def test_lists_audio_sorted_by_title(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ("zeal.mp3", "More Than Able ｜ Piano [_9Evra0kPks].m4a", "notes.txt", "amazing grace.wav"):
                (folder / name).write_bytes(b"x")
            tracks = md.library_tracks(folder)
            self.assertEqual([md.track_title(p) for p in tracks], ["amazing grace", "More Than Able | Piano", "zeal"])

    def test_missing_folder_is_empty(self) -> None:
        self.assertEqual(md.library_tracks(Path("/nonexistent/music")), [])


class TestMusicListInWizard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_clicking_a_song_chooses_it_and_new_files_appear(self) -> None:
        import ui.wizard_window as ww

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "Hosanna [abcdef123].m4a").write_bytes(b"x")
            (folder / "Amazing Grace.mp3").write_bytes(b"x")
            with mock.patch.object(md, "MUSIC_DIR", folder):
                window = ww.WizardWindow()
                window._refresh_music_library()
                lst = window._music_list
                self.assertEqual([lst.item(i).text() for i in range(lst.count())], ["♪  Amazing Grace", "♪  Hosanna"])
                self.assertEqual(lst.currentRow(), -1)

                lst.setCurrentRow(1)
                music = window._project.selected_music
                self.assertEqual((music.title, music.local_path.name), ("Hosanna", "Hosanna [abcdef123].m4a"))

                (folder / "Be Still.mp3").write_bytes(b"x")
                window._refresh_music_library()
                self.assertEqual(lst.count(), 3)
                self.assertEqual(lst.currentItem().text(), "♪  Hosanna")  # choice kept after refresh
                window.close()

    def test_empty_folder_shows_hint(self) -> None:
        import ui.wizard_window as ww

        with TemporaryDirectory() as tmp, mock.patch.object(md, "MUSIC_DIR", Path(tmp)):
            window = ww.WizardWindow()
            window._refresh_music_library()
            self.assertTrue(window._music_list.isHidden())
            self.assertFalse(window._music_empty.isHidden())
            window.close()


if __name__ == "__main__":
    unittest.main()
