from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services import highlights as hl
from services.highlights import Sentence, build_plan, find_sermon_window, sentences
from services.transcribe import Word


def _speech(start: float, end: float, vocab: int = 400) -> list[Word]:
    """Dense, varied speech: one word every 0.4 s, sentences every 10 words."""
    out, t, n = [], start, 0
    while t < end:
        text = f" w{n % vocab}" + ("." if n % 10 == 9 else "")
        out.append(Word(t, t + 0.3, text))
        t += 0.4
        n += 1
    return out


def _song(start: float, end: float) -> list[Word]:
    """What Whisper makes of worship music: sparse repeated 'Amen.'"""
    return [Word(t, t + 0.5, " Amen.") for t in range(int(start), int(end), 15)]


def _sents(lengths: list[float]) -> list[Sentence]:
    out, t = [], 1000.0
    for i, length in enumerate(lengths, start=1):
        out.append(Sentence(i, t, t + length, f"sentence {i}."))
        t += length + 0.3
    return out


class TestSermonWindow(unittest.TestCase):
    def test_finds_longest_dense_stretch(self) -> None:
        words = _song(0, 1800) + _speech(1800, 2400, vocab=40)  # announcements, short
        words += _song(2400, 3000) + _speech(3000, 6600) + _song(6600, 7200)
        start, end = find_sermon_window(sorted(words, key=lambda w: w.start))
        self.assertEqual((start, end), (3000.0, 6600.0))

    def test_repeated_words_are_not_speech(self) -> None:
        amen_flood = [Word(t * 0.4, t * 0.4 + 0.3, " Amen.") for t in range(1000)]
        start, end = find_sermon_window(amen_flood)
        self.assertEqual((start, end), (0.0, amen_flood[-1].end))  # nothing dense: whole thing

    def test_single_quiet_window_does_not_split_sermon(self) -> None:
        words = _speech(0, 1500) + _song(1500, 1800) + _speech(1800, 3300)
        start, end = find_sermon_window(sorted(words, key=lambda w: w.start))
        self.assertEqual(start, 0.0)
        self.assertAlmostEqual(end, 3300.0, delta=1)  # capped at the last word


class TestSentences(unittest.TestCase):
    def test_splits_on_punctuation_pause_and_length(self) -> None:
        words = [Word(0.0, 0.3, " Hello"), Word(0.4, 0.7, " church."), Word(0.8, 1.1, " Amen"),
                 Word(3.0, 3.3, " later")]
        sents = sentences(words, 0, 10)
        self.assertEqual([s.text for s in sents], ["Hello church.", "Amen", "later"])
        self.assertEqual([s.index for s in sents], [1, 2, 3])
        long = [Word(t * 0.5, t * 0.5 + 0.4, f" x{t}") for t in range(60)]
        self.assertTrue(all(s.end - s.start <= 20.0 for s in sentences(long, 0, 100)))


class TestBuildPlan(unittest.TestCase):
    def test_valid_choice_becomes_clean_cuts_in_order(self) -> None:
        sents = _sents([10] * 30)
        data = {
            "sermon_title": "Joseph",
            "highlights": [
                {"start_sentence": 20, "end_sentence": 23, "title": "Ang plano ng Diyos", "reason": "core"},
                {"start_sentence": 3, "end_sentence": 5, "title": "Story", "reason": "story"},
            ],
            "verse": {"reference": "Genesis 50:20", "text": "You intended to harm me...", "reason": "theme"},
        }
        plan = build_plan(data, sents)
        self.assertEqual([h.title for h in plan.highlights], ["Story", "Ang plano ng Diyos"])
        first = plan.highlights[0]
        # Padded, but capped just short of the neighbouring sentences (0.3 s apart here).
        self.assertAlmostEqual(first.start, max(sents[2].start - hl.LEAD_IN_S, sents[1].end + 0.02))
        self.assertAlmostEqual(first.end, min(sents[4].end + hl.TAIL_S, sents[5].start - 0.02))
        self.assertLessEqual(plan.total_s, hl.MAX_TOTAL_S)
        self.assertEqual(plan.verse_reference, "Genesis 50:20")

    def test_long_highlight_is_shortened_from_the_end(self) -> None:
        sents = _sents([10] * 20)
        plan = build_plan({"highlights": [{"start_sentence": 1, "end_sentence": 12, "title": "t", "reason": "r"}]}, sents)
        self.assertLessEqual(plan.highlights[0].duration, hl.MAX_CLIP_S + hl.LEAD_IN_S + hl.TAIL_S)
        self.assertAlmostEqual(plan.highlights[0].start, sents[0].start - hl.LEAD_IN_S)

    def test_total_kept_within_two_minutes(self) -> None:
        sents = _sents([10] * 40)
        picks = [{"start_sentence": a, "end_sentence": a + 4, "title": f"h{a}", "reason": "r"} for a in (1, 11, 21)]
        plan = build_plan({"highlights": picks}, sents)  # 3 × ~51 s asked
        self.assertLessEqual(plan.total_s, hl.MAX_TOTAL_S)
        self.assertGreaterEqual(len(plan.highlights), 2)

    def test_rejects_invalid_overlapping_and_short(self) -> None:
        sents = _sents([10] * 20)
        picks = [
            {"start_sentence": 2, "end_sentence": 5, "title": "good", "reason": "r"},
            {"start_sentence": 4, "end_sentence": 6, "title": "overlap", "reason": "r"},
            {"start_sentence": 99, "end_sentence": 100, "title": "bad ids", "reason": "r"},
        ]
        plan = build_plan({"highlights": picks}, sents)
        self.assertEqual([h.title for h in plan.highlights], ["good"])
        self.assertEqual(len(plan.notes), 2)
        short = build_plan({"highlights": [{"start_sentence": 1, "end_sentence": 1, "title": "s", "reason": "r"}]},
                           _sents([5] * 3))
        self.assertEqual(short.highlights, [])

    def test_padding_never_reaches_neighbouring_words(self) -> None:
        # Next sentence starts only 0.1 s after the chosen one ends.
        sents = [Sentence(1, 100.0, 110.0, "before."), Sentence(2, 110.05, 125.0, "chosen one."),
                 Sentence(3, 125.1, 140.0, "chosen two."), Sentence(4, 140.1, 150.0, "So, next thought.")]
        plan = build_plan({"highlights": [{"start_sentence": 2, "end_sentence": 3, "title": "t", "reason": "r"}]}, sents)
        h = plan.highlights[0]
        self.assertGreaterEqual(h.start, 110.0)  # not into "before."
        self.assertLess(h.end, 140.1)  # not into "So, …"

    def test_drops_dangling_filler_at_the_edges(self) -> None:
        sents = _sents([1, 12, 12, 12, 1])
        sents[0] = Sentence(1, sents[0].start, sents[0].end, "Okay?")
        sents[4] = Sentence(5, sents[4].start, sents[4].end, "So,")
        plan = build_plan({"highlights": [{"start_sentence": 1, "end_sentence": 5, "title": "t", "reason": "r"}]}, sents)
        h = plan.highlights[0]
        self.assertAlmostEqual(h.start, max(sents[1].start - hl.LEAD_IN_S, sents[0].end + 0.02))
        self.assertAlmostEqual(h.end, min(sents[3].end + hl.TAIL_S, sents[4].start - 0.02))

    def test_never_more_than_three(self) -> None:
        sents = _sents([5] * 60)
        picks = [{"start_sentence": a, "end_sentence": a + 3, "title": f"h{a}", "reason": "r"} for a in (1, 10, 20, 30)]
        self.assertLessEqual(len(build_plan({"highlights": picks}, sents).highlights), 3)


class _FakeClaude:
    def __init__(self, payload: dict, stop_reason: str = "end_turn") -> None:
        self.calls: list[dict] = []
        text = SimpleNamespace(type="text", text=json.dumps(payload))
        response = SimpleNamespace(stop_reason=stop_reason, content=[text] if stop_reason == "end_turn" else [])
        create = mock.Mock(side_effect=lambda **kw: self.calls.append(kw) or response)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


class TestAskClaude(unittest.TestCase):
    payload = {
        "sermon_title": "t",
        "highlights": [{"start_sentence": 1, "end_sentence": 3, "title": "a", "reason": "b"}],
        "verse": {"reference": "John 3:16", "text": "For God so loved", "reason": "c"},
    }

    def test_request_uses_opus_fallback_and_schema(self) -> None:
        fake = _FakeClaude(self.payload)
        data = hl.ask_claude(_sents([10] * 5), fake)
        self.assertEqual(data["verse"]["reference"], "John 3:16")
        call = fake.calls[0]
        self.assertEqual(call["model"], "claude-opus-5-5")
        self.assertEqual(call["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(call["fallbacks"], "default")
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        self.assertIn("S1 [0:16:40 +10s] sentence 1.", call["messages"][0]["content"])

    def test_workspace_error_explained(self) -> None:
        import anthropic
        import httpx

        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(400, request=request)
        error = anthropic.BadRequestError(
            "This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header",
            response=response,
            body=None,
        )
        fake = _FakeClaude(self.payload)
        fake.beta.messages.create = mock.Mock(side_effect=error)
        with self.assertRaises(RuntimeError) as ctx:
            hl.ask_claude(_sents([10] * 5), fake)
        self.assertIn("ANTHROPIC_WORKSPACE_ID", str(ctx.exception))

    def test_refusal_is_reported(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            hl.ask_claude(_sents([10] * 5), _FakeClaude(self.payload, stop_reason="refusal"))
        self.assertIn("declined", str(ctx.exception))


class TestFindHighlights(unittest.TestCase):
    def test_end_to_end_with_fake_transcript_and_claude(self) -> None:
        words = sorted(_song(0, 1200) + _speech(1200, 3600) + _song(3600, 4200), key=lambda w: w.start)
        sermon = sentences(words, 1200, 3600)
        payload = {
            "sermon_title": "Joseph",
            "highlights": [
                {"start_sentence": 10, "end_sentence": 14, "title": "one", "reason": "r"},
                {"start_sentence": 100, "end_sentence": 104, "title": "two", "reason": "r"},
            ],
            "verse": {"reference": "Genesis 50:20", "text": "...", "reason": "r"},
        }
        steps: list[float] = []
        with mock.patch.object(hl, "transcribe_service", return_value=words):
            plan = hl.find_highlights(Path("service.mp4"), claude=_FakeClaude(payload),
                                      progress_cb=lambda r, _m: steps.append(r))
        self.assertEqual((plan.sermon_start, plan.sermon_end), (1200.0, 3600.0))
        self.assertEqual([h.title for h in plan.highlights], ["one", "two"])
        self.assertAlmostEqual(
            plan.highlights[0].start, max(sermon[9].start - hl.LEAD_IN_S, sermon[8].end + 0.02)
        )
        self.assertEqual(steps, sorted(steps))

    def test_format_timecode_for_wizard(self) -> None:
        self.assertEqual(hl.format_timecode(3000.5), "00:50:00.50")
        self.assertEqual(hl.format_timecode(4323.754), "01:12:03.75")


if __name__ == "__main__":
    unittest.main()
