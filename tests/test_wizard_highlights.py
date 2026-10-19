from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from services.claude_client import MissingApiKey
from services.highlights import Highlight, HighlightPlan, LuxUsage


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
                Highlight(3010.0, 3048.0, "Ang plano ng Diyos", "core message"),
                Highlight(4321.0, 4368.0, "Joseph forgives", "story"),
            ],
            verse_reference="Genesis 50:20",
            verse_text="You intended to harm me, but God intended it for good.",
            verse_reason="the sermon's main text",
            sermon_title="Joseph",
            sermon_start=2400,
            sermon_end=6600,
            usage=LuxUsage("claude-opus-5-5", input_tokens=34_701, output_tokens=1_463),
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
                [("00:50:10", "00:50:48", "Ang plano ng Diyos"), ("01:12:01", "01:12:48", "Joseph forgives")],
            )
            self.assertEqual(window._segment_list.count(), 2)
            self.assertEqual(window._verse_ref_edit.text(), "Genesis 50:20")
            self.assertIn("Lux used 34,701 input + 1,463 output tokens · $0.168", window._highlights_summary.text())
            self.assertEqual(finder.call_args.kwargs["ledger"], ww.LUX_LEDGER)
            self.assertTrue(window._lux_spend.text().startswith("Lux spending so far: $"))
            self.assertTrue(window._verse_edit.toPlainText().startswith("You intended"))
            self.assertEqual(window._project.selected_verse.reference, "Genesis 50:20")
            self.assertIn("Total 85s", window._highlights_summary.text())
            window.close()

    def test_pasted_times_become_60s_ranges_lux_scans(self) -> None:
        import ui.wizard_window as ww

        plan = HighlightPlan(highlights=[Highlight(3670.0, 3700.0, "Grace", "core")], usage=None)
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "service.mp4"
            video.write_bytes(b"x")
            window = ww.WizardWindow()
            window._project.sermon_path = video
            with mock.patch.object(ww, "api_key", return_value="sk-test"), mock.patch.object(
                ww.QInputDialog, "getMultiLineText", return_value=("101\n112\n124\n149\n151", True)
            ), mock.patch.object(ww.QMessageBox, "question") as asked, mock.patch.object(
                ww, "find_highlights", return_value=plan
            ) as finder:
                window._paste_timestamps()
                self._wait(window)
            asked.assert_not_called()
            self.assertEqual(
                finder.call_args.kwargs["ranges"],
                [(3660.0, 3720.0), (4320.0, 4380.0), (5040.0, 5100.0), (6540.0, 6600.0), (6660.0, 6720.0)],
            )
            self.assertEqual([s.label for s in window._project.segments], ["Grace"])
            window.close()

    def test_find_sermon_adds_one_adjustable_clip(self) -> None:
        import ui.wizard_window as ww

        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "service.mp4"
            video.write_bytes(b"x")
            window = ww.WizardWindow()
            window._project.sermon_path = video
            with mock.patch.object(ww, "find_sermon", return_value=(2253.0, 6735.0)) as finder, mock.patch.object(
                ww, "api_key", side_effect=AssertionError("Find sermon must not need Lux")
            ):
                window._find_sermon()
                self._wait(window)
            finder.assert_called_once()
            segments = [(s.start_text, s.end_text, s.label) for s in window._project.segments]
            self.assertEqual(segments, [("00:37:33", "01:52:15", "Sermon")])
            self.assertEqual(window._segment_list.currentRow(), 0)
            self.assertEqual(window._seg_start.text(), "00:37:33")
            self.assertIn("Sermon found", window._highlights_summary.text())
            window.close()

    def test_trimmed_clips_become_the_scan_range(self) -> None:
        import ui.wizard_window as ww
        from models.project import ClipSegment

        plan = HighlightPlan(highlights=[Highlight(3010.0, 3045.0, "Pick", "why")])
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "service.mp4"
            video.write_bytes(b"x")
            window = ww.WizardWindow()
            window._project.sermon_path = video
            window._project.segments = [ClipSegment("00:40:00", "01:50:00", "Sermon")]
            with mock.patch.object(ww, "api_key", return_value="sk-test"), mock.patch.object(
                ww, "find_highlights", return_value=plan
            ) as finder, mock.patch.object(
                ww.QMessageBox, "question", return_value=ww.QMessageBox.StandardButton.Yes
            ) as asked:
                window._find_highlights()
                self._wait(window)
            self.assertIn("only inside your trimmed clips", asked.call_args.args[2])
            self.assertEqual(finder.call_args.kwargs["ranges"], [(2400.0, 6600.0)])
            self.assertEqual([s.label for s in window._project.segments], ["Pick"])
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
