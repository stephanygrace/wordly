from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from services import photo_downloader as pd
from services.resolve_export import END_PHOTO_S, _build_fcpxml, _seconds_to_fcpxml
from services.reel_layout import VerseLine

PAGE = (
    b'<html><head><meta property="og:image" content="https://scontent.example/v/p.jpg?stp=dst-jpg_tt6'
    b'&amp;cstp=mx1080x1350&amp;ctp=p960x960&amp;oh=sig" /></head></html>'
)
LINK = "https://www.facebook.com/photo/?fbid=1433706522300134&set=a.398561329147997"


class TestPhotoDownload(unittest.TestCase):
    def test_full_size_photo_from_facebook_link(self) -> None:
        asked: list[tuple[str, str]] = []

        def fetch(url, agent):
            asked.append((url, agent))
            return (PAGE, "text/html") if "facebook.com" in url else (b"JPEG", "image/jpeg")

        with TemporaryDirectory() as tmp:
            path = pd.download_photo(LINK, dest_dir=Path(tmp), fetch=fetch)
            self.assertEqual(path.name, "fb_1433706522300134.jpg")
            self.assertEqual(path.read_bytes(), b"JPEG")
        self.assertTrue(asked[0][1].startswith("WhatsApp"))  # previews get the og:image
        # The preview size hint is dropped to get the full upload.
        self.assertEqual(asked[1][0], "https://scontent.example/v/p.jpg?stp=dst-jpg_tt6&cstp=mx1080x1350&oh=sig")

    def test_direct_image_link_and_friendly_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            path = pd.download_photo(
                "https://example.com/poster.png", dest_dir=Path(tmp), fetch=lambda u, a: (b"PNG", "image/png")
            )
            self.assertEqual(path.suffix, ".png")
            with self.assertRaisesRegex(RuntimeError, "Choose image"):
                pd.download_photo(LINK, dest_dir=Path(tmp), fetch=lambda u, a: (b"<html>login</html>", "text/html"))
            with self.assertRaisesRegex(ValueError, "https://"):
                pd.download_photo("not a link", dest_dir=Path(tmp))


class TestEndPhotoOnTimeline(unittest.TestCase):
    def test_last_clip_fades_into_end_photo_and_music_ends_with_it(self) -> None:
        clips = [Path("/tmp/Clip001.mp4"), Path("/tmp/Clip002.mp4"), Path("/tmp/EndPhoto.mp4")]
        with patch("services.resolve_export._safe_duration", side_effect=[13.0, 11.0, 6.0, 120.0]):
            root = _build_fcpxml(
                project_name="p",
                clip_paths=clips,
                music=Path("/tmp/song.m4a"),
                timeline_duration_s=27.0,  # 12 + 10 + 5
                verse=[VerseLine("John 3:16", True, -9.5)],
                handle_s=0.5,
                verse_duration_s=22.0,
            )
        spine = list(root.find(".//spine"))
        self.assertEqual([el.tag for el in spine][-2:], ["transition", "asset-clip"])
        end = spine[-1]
        self.assertEqual(end.get("name"), "End photo")
        self.assertEqual((end.get("offset"), end.get("duration")), (_seconds_to_fcpxml(22), _seconds_to_fcpxml(END_PHOTO_S)))
        first = spine[0]
        connected = {el.get("name"): el for el in first}
        # Music runs to the end of the photo and stops there; the verse stays over the sermon.
        self.assertEqual(connected["Instrumental"].get("duration"), _seconds_to_fcpxml(27))
        self.assertEqual(connected["Bible verse"].get("duration"), _seconds_to_fcpxml(22))


class TestEndPhotoInWizard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_photo_link_sets_end_photo_with_preview(self) -> None:
        import ui.wizard_window as ww
        from PySide6.QtGui import QColor, QImage

        with TemporaryDirectory() as tmp:
            photo = Path(tmp) / "fb_1.jpg"
            image = QImage(108, 135, QImage.Format.Format_RGB32)
            image.fill(QColor("red"))
            image.save(str(photo))
            window = ww.WizardWindow()
            window._end_photo_url_edit.setText(LINK)
            with patch.object(ww, "download_photo", return_value=photo) as download:
                window._download_end_photo()
                deadline = __import__("time").time() + 5
                while window._busy and __import__("time").time() < deadline:
                    self.app.processEvents()
                self.app.processEvents()
            download.assert_called_once_with(LINK)
            self.assertEqual(window._project.end_photo, photo)
            self.assertFalse(window._end_photo_preview.pixmap().isNull())
            window._refresh_export_page()
            self.assertIn("fb_1.jpg", window._summary_rows["end_photo"]._value.text())
            window._set_end_photo(None)
            self.assertIsNone(window._project.end_photo)
            self.assertTrue(window._end_photo_preview.isHidden())
            window.close()


if __name__ == "__main__":
    unittest.main()
