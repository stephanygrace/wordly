from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from services.transcribe import (
    Caption,
    Word,
    apply_replacements,
    captions_for_clip,
    chunk_words,
    load_replacements,
)


def _words(spec: list[tuple[float, float, str]]) -> list[Word]:
    return [Word(s, e, t) for s, e, t in spec]


class TestChunkWords(unittest.TestCase):
    def test_long_phrase_split_evenly(self) -> None:
        words = _words([(i * 0.3, i * 0.3 + 0.25, f" w{i}") for i in range(7)])
        caps = chunk_words(words, max_words=5)
        # Balanced 4 + 3 instead of 5 + 2.
        self.assertEqual([c.text for c in caps], ["w0 w1 w2 w3", "w4 w5 w6"])

    def test_splits_on_pause_and_sentence_end(self) -> None:
        words = _words([
            (0.0, 0.3, " God"),
            (0.3, 0.6, " is"),
            (0.6, 0.9, " good."),
            (1.0, 1.3, " All"),
            (1.3, 1.6, " the"),
            (2.6, 2.9, " time"),
        ])
        caps = chunk_words(words)
        self.assertEqual([c.text for c in caps], ["God is good", "All the", "time"])

    def test_keeps_question_mark_and_does_not_overlap(self) -> None:
        words = _words([(0.0, 0.1, " Why?"), (0.15, 0.4, " Because")])
        caps = chunk_words(words)
        self.assertEqual(caps[0].text, "Why?")
        # Minimum caption length would overlap the next caption, so it is capped.
        self.assertLessEqual(caps[0].end, caps[1].start)

    def test_splits_long_duration(self) -> None:
        words = _words([(0.0, 1.0, " Slowly"), (1.0, 2.0, " spoken"), (2.0, 3.5, " words")])
        caps = chunk_words(words, max_duration_s=3.0)
        self.assertEqual([c.text for c in caps], ["Slowly spoken", "words"])


class TestReplacements(unittest.TestCase):
    table = {("first", "corinthians"): "1 Corinthians", ("holy", "spirit"): "Holy Spirit"}

    def test_replaces_phrase_and_keeps_timing_and_punctuation(self) -> None:
        words = _words([(0.0, 0.2, " Read"), (0.2, 0.5, " First"), (0.5, 1.0, " Corinthians,")])
        out = apply_replacements(words, self.table)
        self.assertEqual([w.text for w in out], [" Read", " 1 Corinthians,"])
        self.assertEqual((out[1].start, out[1].end), (0.2, 1.0))

    def test_skips_match_broken_by_punctuation(self) -> None:
        words = _words([(0.0, 0.2, " first,"), (0.2, 0.5, " Corinthians")])
        out = apply_replacements(words, self.table)
        self.assertEqual(out, words)

    def test_repo_replacements_file_loads(self) -> None:
        table = load_replacements()
        self.assertEqual(table[("first", "corinthians")], "1 Corinthians")
        self.assertNotIn(("_comment",), table)

    def test_load_custom_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps({"Pastor Jon": "Ptr. Jon"}), encoding="utf-8")
            table = load_replacements(path)
        self.assertEqual(table[("pastor", "jon")], "Ptr. Jon")
        # The written form maps to itself so it is kept as one unit.
        self.assertEqual(table[("ptr", "jon")], "Ptr. Jon")

    def test_written_form_is_merged_into_one_word(self) -> None:
        words = _words([
            (0.0, 0.2, " Turn"), (0.2, 0.4, " with"), (0.4, 0.5, " me"), (0.5, 0.6, " to"),
            (0.6, 0.8, " 1"), (0.8, 1.2, " Corinthians"), (1.2, 1.5, " 13."),
        ])
        out = apply_replacements(words, load_replacements())
        caps = chunk_words(out)
        self.assertTrue(any("1 Corinthians" in c.text for c in caps), caps)


class TestCaptionsForClip(unittest.TestCase):
    def test_drops_and_trims_captions_past_clip_end(self) -> None:
        words = _words([
            (0.0, 0.5, " Hello."),
            (4.8, 5.4, " Cut."),
            (6.0, 6.5, " Gone."),
        ])
        with mock.patch("services.transcribe.transcribe_words", return_value=words):
            caps = captions_for_clip(Path("clip.mp4"), duration_s=5.0, replacements={})
        self.assertEqual(caps, [Caption(0.0, 0.5, "Hello"), Caption(4.8, 5.0, "Cut")])



class TestLanguage(unittest.TestCase):
    def test_defaults_to_tagalog_and_auto_disables(self) -> None:
        import os

        from services.transcribe import whisper_language

        with mock.patch.dict(os.environ, {"WORDLY_WHISPER_LANGUAGE": ""}):
            self.assertEqual(whisper_language(), "tl")
        with mock.patch.dict(os.environ, {"WORDLY_WHISPER_LANGUAGE": "auto"}):
            self.assertIsNone(whisper_language())
        with mock.patch.dict(os.environ, {"WORDLY_WHISPER_LANGUAGE": "en"}):
            self.assertEqual(whisper_language(), "en")

if __name__ == "__main__":
    unittest.main()
