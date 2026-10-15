from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from services.claude_client import MissingApiKey
from services.highlights import Highlight, HighlightPlan


class TestFindHighlightsInWizard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _wait(self, window, limit: float = 10.0) -> None:
        deadline = time.time() + limit
        while window._busy and time.time() < deadline:
            self.app.processEvents()
        self.app.processEvents()

    def test_button_fills_segments_and_verse(self) -> None:
        import ui.wizard_window as ww

        plan = HighlightPlan(
            highlights=[
                Highlight(3010.25, 3048.5, "Ang plano ng Diyos", "core message"),
                Highlight(4321.0, 4368.0, "Joseph forgives", "story"),
            ],
            verse_reference="Genesis 50:20",
            verse_text="You intended to harm me, but God intended it for good.",
            verse_reason="the sermon's main text",
            sermon_title="Joseph",
            sermon_start=2400,
            sermon_end=6600,
        )
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "service.mp4"
            video.write_bytes(b"x")
            window = ww.WizardWindow()
            window._project.sermon_path = video
            with mock.patch.object(ww, "api_key", return_value="sk-test"), mock.patch.object(
                ww, "find_highlights", return_value=plan
            ) as finder:
                window._find_highlights()
                self._wait(window)
            finder.assert_called_once()
            segments = [(s.start_text, s.end_text, s.label) for s in window._project.segments]
            self.assertEqual(
                segments,
                [("00:50:10.25", "00:50:48.50", "Ang plano ng Diyos"), ("01:12:01.00", "01:12:48.00", "Joseph forgives")],
            )
            self.assertEqual(window._segment_list.count(), 2)
            self.assertEqual(window._verse_edit.toPlainText().splitlines()[0], "Genesis 50:20")
            self.assertEqual(window._project.selected_verse.reference, "Genesis 50:20")
            self.assertIn("Total 85s", window._highlights_summary.text())
            window.close()

    def test_asks_for_key_once_and_saves_it(self) -> None:
        import ui.wizard_window as ww

        window = ww.WizardWindow()
        with mock.patch.object(ww, "api_key", side_effect=MissingApiKey("none")), mock.patch.object(
            ww.QInputDialog, "getText", return_value=("sk-ant-new", True)
        ), mock.patch.object(ww, "save_api_key") as save:
            self.assertTrue(window._ensure_api_key())
        save.assert_called_once_with("sk-ant-new")
        with mock.patch.object(ww, "api_key", side_effect=MissingApiKey("none")), mock.patch.object(
            ww.QInputDialog, "getText", return_value=("", False)
        ):
            self.assertFalse(window._ensure_api_key())
        window.close()


if __name__ == "__main__":
    unittest.main()
