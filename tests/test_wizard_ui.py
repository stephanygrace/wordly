from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class TestWizardUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        import ui.wizard_window as ww

        self.ww = ww
        self.window = ww.WizardWindow()

    def tearDown(self) -> None:
        self.window.close()

    def _type_clip(self, start: str, end: str, title: str = "") -> None:
        w = self.window
        w._seg_start.setText(start)
        w._seg_end.setText(end)
        w._seg_label.setText(title)

    def test_add_edit_and_remove_clips(self) -> None:
        w = self.window
        self._type_clip("00:10:00", "00:10:30", "Opening")
        self.assertTrue(w._add_segment_btn.isEnabled())
        w._add_segment()
        self._type_clip("00:20:00", "00:20:45")
        w._add_segment()
        self.assertEqual(w._segment_list.count(), 2)
        self.assertEqual(w._seg_start.text(), "")

        w._segment_list.setCurrentRow(1)
        self.assertEqual(w._add_segment_btn.text(), "Save changes")
        self.assertEqual(w._seg_start.text(), "00:20:00")
        w._seg_label.setText("Closing")
        w._add_segment()
        self.assertEqual(len(w._project.segments), 2)
        self.assertEqual(w._project.segments[1].label, "Closing")
        self.assertEqual(w._add_segment_btn.text(), "Add clip")
        self.assertIn("75s", w._clips_total_pill.text())

        w._segment_list.setCurrentRow(0)
        w._remove_segment()
        self.assertEqual([s.label for s in w._project.segments], ["Closing"])

    def test_invalid_range_disables_add(self) -> None:
        w = self.window
        self._type_clip("00:10:30", "00:10:00")
        self.assertFalse(w._add_segment_btn.isEnabled())
        self.assertFalse(w._seg_error.isHidden())
        self.assertIn("End time must be greater", w._seg_error.text())

    def test_fractional_timecode_is_kept(self) -> None:
        w = self.window
        w._seg_start.setText("00:50:10.25")
        self.assertEqual(w._seg_start.text(), "00:50:10.25")

    def test_pasted_reference_moves_to_reference_field(self) -> None:
        w = self.window
        w._verse_edit.setPlainText("John 3:16\nFor God so loved the world.")
        self.assertEqual(w._verse_ref_edit.text(), "John 3:16")
        self.assertEqual(w._verse_edit.toPlainText(), "For God so loved the world.")
        self.assertEqual(w._project.selected_verse.reference, "John 3:16")

    def test_sidebar_only_opens_reachable_steps(self) -> None:
        w = self.window
        w._go_to_step(self.ww.S_EXPORT)
        self.assertEqual(w._stack.currentIndex(), self.ww.S_SOURCE)
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "service.mp4"
            video.write_bytes(b"x")
            w._project.sermon_path = video
            w._go_to_step(self.ww.S_HIGHLIGHTS)
            self.assertEqual(w._stack.currentIndex(), self.ww.S_HIGHLIGHTS)
            self._type_clip("00:00:01", "00:00:05")
            w._add_segment()
            # Clips aren't cut yet, so later steps stay locked.
            w._go_to_step(self.ww.S_CAPTIONS)
            self.assertEqual(w._stack.currentIndex(), self.ww.S_HIGHLIGHTS)
            self.assertTrue(w._next_btn.text().startswith("Cut clips"))
            w._go_to_step(self.ww.S_SOURCE)
            self.assertEqual(w._stack.currentIndex(), self.ww.S_SOURCE)


if __name__ == "__main__":
    unittest.main()
