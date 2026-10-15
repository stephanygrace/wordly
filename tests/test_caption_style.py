from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from services.caption_style import (
    DEFAULT_STYLE_NAME,
    get_caption_style,
    hex_to_fcpxml,
    load_caption_styles,
)
from services.transcribe import Caption, Word, word_timings


class TestCaptionStyles(unittest.TestCase):
    def test_repo_presets(self) -> None:
        styles = load_caption_styles()
        reel = styles[DEFAULT_STYLE_NAME]
        self.assertEqual(reel.name, "Sermon reel")
        self.assertTrue(reel.uppercase)
        self.assertEqual(reel.highlight_color, "#FFF000")
        self.assertEqual((reel.outline_width, reel.max_words), (0, 3))
        self.assertEqual((reel.font, reel.font_face, reel.font_size), ("Outfit", "Bold", 24))
        self.assertEqual(styles["Word highlight"].highlight_color, "#FFD400")
        self.assertIsNone(styles["Clean white"].highlight_color)

    def test_custom_style_and_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "styles.json"
            path.write_text(
                json.dumps({"styles": [{"name": "Church red", "highlight_color": "#CC0000", "unknown": 1}]}),
                encoding="utf-8",
            )
            self.assertEqual(get_caption_style("Church red", path).highlight_color, "#CC0000")
            self.assertEqual(get_caption_style("Missing", path).name, DEFAULT_STYLE_NAME)

    def test_bad_colour_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "styles.json"
            path.write_text(json.dumps({"styles": [{"name": "Oops", "text_color": "white"}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_caption_styles(path)

    def test_hex_to_fcpxml(self) -> None:
        self.assertEqual(hex_to_fcpxml("#FFFFFF"), "1 1 1 1")
        self.assertEqual(hex_to_fcpxml("000000"), "0 0 0 1")
        self.assertEqual(hex_to_fcpxml("#FF000080"), "1 0 0 0.502")


class TestWordTimings(unittest.TestCase):
    def test_uses_whisper_words_and_is_contiguous(self) -> None:
        cap = Caption(0.0, 1.5, "God is good", (Word(0.1, 0.4, " God"), Word(0.5, 0.7, " is"), Word(0.9, 1.2, " good.")))
        self.assertEqual(
            word_timings(cap),
            [("God", 0.0, 0.5), ("is", 0.5, 0.9), ("good", 0.9, 1.5)],
        )

    def test_merged_word_shares_its_span(self) -> None:
        cap = Caption(0.0, 1.0, "to 1 Corinthians", (Word(0.0, 0.2, " to"), Word(0.2, 1.0, " 1 Corinthians")))
        tokens = word_timings(cap)
        self.assertEqual([t for t, _, _ in tokens], ["to", "1", "Corinthians"])
        self.assertAlmostEqual(tokens[2][1], 0.6)

    def test_edited_text_is_spread_by_length(self) -> None:
        cap = Caption(0.0, 1.0, "Amen church", (Word(0.0, 1.0, " Amen"),))
        tokens = word_timings(cap)
        self.assertEqual([t for t, _, _ in tokens], ["Amen", "church"])
        self.assertAlmostEqual(tokens[1][1], 0.4)
        self.assertEqual(tokens[-1][2], 1.0)


if __name__ == "__main__":
    unittest.main()
