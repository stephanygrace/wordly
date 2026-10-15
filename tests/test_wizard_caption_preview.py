from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from services.transcribe import Caption, Word


def _texts(window):
    return [(round(c.start, 2), round(c.end, 2), c.text) for c in window._project.clip_captions[0]]


def ww_module():
    import ui.wizard_window as ww

    return ww


class TestCaptionPreviewFollowsClip(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _window(self, tmp: str):
        import ui.wizard_window as ww

        clips = [Path(tmp) / "Clip001.mp4", Path(tmp) / "Clip002.mp4"]
        for clip in clips:
            clip.write_bytes(b"x")
        window = ww.WizardWindow()
        window._auto_captions_check.setChecked(True)
        window._project.clip_paths = list(clips)
        window._project.captions_source = list(clips)
        window._project.clip_captions = [
            [
                Caption(0.0, 1.0, "Naroon ka", (Word(0.0, 0.5, "Naroon"), Word(0.5, 1.0, "ka"))),
                Caption(1.2, 2.0, "na ba?", (Word(1.2, 1.6, "na"), Word(1.6, 2.0, "ba?"))),
            ],
            [Caption(0.0, 1.5, "God is good", ())],
        ]
        return window, clips

    def test_playback_selects_row_and_lights_spoken_word(self) -> None:
        with TemporaryDirectory() as tmp:
            window, clips = self._window(tmp)
            with mock.patch.object(window._caption_player, "load_file") as load:
                window._refresh_captions_step()
                load.assert_called_once_with(clips[0])
                window._on_caption_player_position(1700)  # "ba?" in the 2nd caption
            self.assertEqual(window._captions_table.currentRow(), 1)
            html = window._caption_preview.text()
            style = window._selected_caption_style()
            self.assertIn(f'color:{style.highlight_color}">{style.display_text("ba?")}', html)
            self.assertIn(f'color:{style.text_color}">{style.display_text("na")}', html)
            window.close()

    def test_clicking_a_caption_plays_its_clip_from_its_start(self) -> None:
        with TemporaryDirectory() as tmp:
            window, clips = self._window(tmp)
            player = window._caption_player
            with mock.patch.object(player, "load_file") as load, mock.patch.object(
                player, "seek_seconds"
            ) as seek, mock.patch.object(player, "play") as play:
                window._refresh_captions_step()
                window._captions_table.setCurrentCell(2, 2)  # clip 2's caption
            load.assert_called_with(clips[1])
            seek.assert_called_with(0.0)
            play.assert_called()
            self.assertIn(window._selected_caption_style().display_text("God"), window._caption_preview.text())
            window.close()

    def test_space_on_a_selected_row_toggles_playback_not_the_text(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        with TemporaryDirectory() as tmp:
            window, _ = self._window(tmp)
            player = window._caption_player
            table = window._captions_table
            with mock.patch.object(player, "load_file"), mock.patch.object(player, "seek_seconds"), mock.patch.object(
                player, "play"
            ), mock.patch.object(player, "toggle_play_pause") as toggle:
                window._refresh_captions_step()
                window.show()
                table.setCurrentCell(1, 2)
                table.setFocus()
                QTest.keyClick(table, Qt.Key.Key_Space)
                QTest.keyClick(table, Qt.Key.Key_A)  # typing doesn't start an edit either
                self.app.processEvents()
            self.assertEqual(toggle.call_count, 1)
            self.assertEqual(table.item(1, 2).text(), "na ba?")
            self.assertEqual(window._project.clip_captions[0][1].text, "na ba?")
            window.close()

    def _ready(self, window):
        player = window._caption_player
        patches = [mock.patch.object(player, name) for name in ("load_file", "seek_seconds", "play", "pause")]
        patches.append(mock.patch.object(window, "_clip_duration", return_value=10.0))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        window._refresh_captions_step()

    def test_insert_line_at_the_paused_playhead_gap(self) -> None:
        with TemporaryDirectory() as tmp:
            window, _ = self._window(tmp)
            self._ready(window)
            window._caption_clip_idx = 0
            with mock.patch.object(window._caption_player, "position_seconds", return_value=4.0):
                window._insert_caption_line()
            self.app.processEvents()
            self.assertEqual(_texts(window)[2], (3.7, 6.7, ""))
            item = window._captions_table.item(2, 2)
            item.setText("ako ay naglalakad")
            self.assertEqual(_texts(window)[2], (3.7, 6.7, "ako ay naglalakad"))
            self.assertIn("4 captions", window._captions_status.text())
            window.close()

    def test_insert_after_selected_line_and_long_text_splits(self) -> None:
        with TemporaryDirectory() as tmp:
            window, _ = self._window(tmp)
            self._ready(window)
            table = window._captions_table
            table.blockSignals(True)
            table.setCurrentCell(1, 2)  # "na ba?" ends at 2.0; the clip runs to 10 s
            table.blockSignals(False)
            window._insert_caption_line()
            self.app.processEvents()
            self.assertEqual(_texts(window)[2], (2.0, 5.0, ""))
            table.item(2, 2).setText("one two three four five six")
            self.app.processEvents()
            self.assertEqual(
                _texts(window)[2:], [(2.0, 3.5, "one two three"), (3.5, 5.0, "four five six")]
            )
            self.assertEqual(table.rowCount(), 5)  # 4 in clip 1 + 1 in clip 2
            window.close()

    def test_no_gap_splits_the_selected_caption_time(self) -> None:
        with TemporaryDirectory() as tmp:
            window, _ = self._window(tmp)
            window._project.clip_captions[0][1] = Caption(1.0, 2.0, "na ba?")  # touches "Naroon ka"
            self._ready(window)
            table = window._captions_table
            table.blockSignals(True)
            table.setCurrentCell(0, 2)
            table.blockSignals(False)
            window._insert_caption_line()
            self.app.processEvents()
            self.assertEqual(_texts(window)[:2], [(0.0, 0.5, "Naroon ka"), (0.5, 1.0, "")])
            window.close()

    def test_time_edit_validates_and_delete_removes(self) -> None:
        with TemporaryDirectory() as tmp:
            window, _ = self._window(tmp)
            self._ready(window)
            table = window._captions_table
            table.item(1, 1).setText("1.1 - 2.5")
            self.app.processEvents()
            self.assertEqual(_texts(window)[1], (1.1, 2.5, "na ba?"))
            with mock.patch.object(ww_module().QMessageBox, "warning") as warn:
                table.item(1, 1).setText("0.5-2.5")  # overlaps "Naroon ka"
                self.app.processEvents()
            warn.assert_called_once()
            self.assertEqual(_texts(window)[1], (1.1, 2.5, "na ba?"))
            self.assertEqual(table.item(1, 1).text().strip(), "1.1–2.5s")
            table.setCurrentCell(0, 2)
            window._delete_caption_line()
            self.assertEqual([c[2] for c in _texts(window)], ["na ba?"])
            window.close()

    def test_new_clips_reset_the_player(self) -> None:
        with TemporaryDirectory() as tmp:
            window, _ = self._window(tmp)
            with mock.patch.object(window._caption_player, "load_file"):
                window._refresh_captions_step()
            window._project.clip_paths = []
            window._refresh_captions_step()
            self.assertIsNone(window._caption_clip_idx)
            self.assertIsNone(window._live_caption)
            window.close()


if __name__ == "__main__":
    unittest.main()
