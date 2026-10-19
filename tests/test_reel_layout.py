from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from services.reel_layout import DEFAULT_LAYOUT, background_filter, caption_position_y, verse_lines


class TestReelGeometry(unittest.TestCase):
    def test_video_band_matches_template(self) -> None:
        # 16:9 at full width in a 1080x1920 frame: y 656..1264, as in the template reel.
        self.assertEqual((DEFAULT_LAYOUT.video_top, DEFAULT_LAYOUT.video_bottom), (656, 1264))

    def test_caption_sits_under_video(self) -> None:
        # Template caption centre ~y=1385 → 22.1% below frame centre.
        self.assertEqual(caption_position_y(), -22.1)

    def test_background_filter_covers_blurs_darkens(self) -> None:
        f = background_filter()
        self.assertIn("force_original_aspect_ratio=increase", f)
        self.assertIn("gblur", f)
        self.assertIn("scale=1080:1920", f)
        self.assertIn("colorchannelmixer=rr=0.22", f)



class TestBackgroundRender(unittest.TestCase):
    def test_full_render_with_changing_zoom(self) -> None:
        """Regression: ffmpeg aborted when the crop size changed mid-clip."""
        import subprocess
        from unittest import mock

        from services import reel_layout, speaker_track

        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=1280x720:d=3:r=30", str(src)],
                check=True,
            )
            # Speaker walking left to right while the zoom swings between body sizes.
            path = [(0.2 + 0.05 * i, 0.5, 0.4 if i % 2 else 0.9) for i in range(12)]
            with mock.patch.object(speaker_track, "vision_available", return_value=True), mock.patch.object(
                speaker_track, "sample_detections", return_value=[[]] * 12
            ), mock.patch.object(speaker_track, "camera_path", return_value=path):
                dest = reel_layout.render_background(src, Path(tmp) / "bg.mp4")
            out = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                    "-show_entries", "stream=width,height,nb_read_frames", "-of", "csv=p=0", str(dest),
                ],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        self.assertEqual(out, "1080,1920,90")



class TestStabilisation(unittest.TestCase):
    @staticmethod
    def _trf(shifts: list[tuple[int, int]]) -> str:
        lines = ["VID.STAB 1", "Frame 1 (List 0 [])"]
        for i, (dx, dy) in enumerate(shifts, start=2):
            lms = ",".join(f"(LM {dx} {dy} {100 + 40 * k} 144 72 0.8 0.5)" for k in range(12))
            lines.append(f"Frame {i} (List 12 [{lms}])")
        return "\n".join(lines) + "\n"

    def test_tripod_footage_has_no_shake(self) -> None:
        from services.reel_layout import camera_shake

        self.assertEqual(camera_shake(self._trf([(0, 0)] * 50)), 0.0)

    def test_handheld_footage_measures_shake(self) -> None:
        from services.reel_layout import SHAKE_THRESHOLD_PX, camera_shake

        shaky = [((-1) ** i * 3, (-1) ** (i // 2) * 2) for i in range(50)]
        self.assertGreaterEqual(camera_shake(self._trf(shaky)), SHAKE_THRESHOLD_PX)

    def test_steady_clip_is_returned_untouched(self) -> None:
        import subprocess

        from services.reel_layout import stabilize_if_shaky

        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "steady.mp4"
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x180:d=2:r=30", str(src)],
                check=True,
            )
            out = stabilize_if_shaky(src, Path(tmp) / "stab.mp4")
            self.assertEqual(out, src)
            self.assertFalse((Path(tmp) / "stab.mp4").exists())


class TestVerseLines(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        # QApplication (not QGuiApplication) so wizard-window tests can share it.
        cls.app = QApplication.instance() or QApplication([])

    def test_wraps_within_the_template_width_at_size_18(self) -> None:
        self.assertEqual(DEFAULT_LAYOUT.verse_resolve_size, 18)
        lines = verse_lines(
            "1 Corinthians 2:9",
            "However, as it is written: \u201cWhat no eye has seen, what no ear has heard, and what no "
            "human mind has conceived\u201d\u2014 the things God has prepared for those who love Him",
        )
        self.assertEqual(
            [line.text for line in lines],
            [
                "1 Corinthians 2:9",
                "However, as it is written: \u201cWhat no eye has seen,",
                "what no ear has heard, and what no human mind",
                "has conceived\u201d\u2014 the things God has prepared for",
                "those who love Him",
            ],
        )
        self.assertEqual([line.bold for line in lines], [True, False, False, False, False])
        # The whole block stays on screen above the video.
        top_centre = DEFAULT_LAYOUT.height / 2 - lines[0].position_y / 100 * DEFAULT_LAYOUT.height
        self.assertGreater(top_centre - DEFAULT_LAYOUT.verse_line_px / 2, 0)

    def test_lines_stack_above_the_video(self) -> None:
        lines = verse_lines("John 3:16", "For God so loved the world")
        ys = [line.position_y for line in lines]
        self.assertEqual(ys, sorted(ys, reverse=True))  # top to bottom
        gap = (ys[0] - ys[1]) / 100 * DEFAULT_LAYOUT.height
        self.assertAlmostEqual(gap, DEFAULT_LAYOUT.verse_line_px, delta=0.5)
        # Last line ends verse_gap_px above the top of the video.
        bottom = DEFAULT_LAYOUT.height / 2 - ys[-1] / 100 * DEFAULT_LAYOUT.height + DEFAULT_LAYOUT.verse_line_px / 2
        self.assertAlmostEqual(bottom, DEFAULT_LAYOUT.video_top - DEFAULT_LAYOUT.verse_gap_px, delta=1)


class TestBundledFonts(unittest.TestCase):
    def test_outfit_bundled_and_installable(self) -> None:
        from utils.fonts import bundled_fonts, install_fonts_for_resolve

        names = {p.name for p in bundled_fonts()}
        self.assertTrue({"Outfit-Bold.ttf", "Outfit-SemiBold.ttf"} <= names)
        with TemporaryDirectory() as tmp:
            first = install_fonts_for_resolve(Path(tmp))
            again = install_fonts_for_resolve(Path(tmp))
        self.assertEqual(len(first), len(names))
        self.assertEqual(again, [])

    def test_qt_can_use_bundled_outfit(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        from utils.fonts import register_bundled_fonts

        register_bundled_fonts()
        self.assertIn("Outfit", QFontDatabase.families())


if __name__ == "__main__":
    unittest.main()
