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


class TestLocateSermon(unittest.TestCase):
    def _lyrics(self, start: float, end: float) -> list[Word]:
        """A worship song: plenty of words, but the same few lines over and over."""
        line = [" I", " want", " to", " know", " you", " more."]
        return [Word(t, t + 0.3, line[k % len(line)]) for k, t in enumerate(range(int(start), int(end)))]

    def test_cuts_out_songs_at_the_first_spoken_word(self) -> None:
        words = self._lyrics(0, 1500) + _speech(1510, 4200) + self._lyrics(4210, 4800)
        start, end = hl.locate_sermon(sorted(words, key=lambda w: w.start))
        self.assertEqual(start, 1509.0)  # the whole second before the first spoken word
        self.assertAlmostEqual(end, 4200, delta=1.0)

    def test_organ_flood_and_a_garbled_minute_do_not_end_the_sermon(self) -> None:
        organ = [Word(1800 + k * 0.1, 1800 + k * 0.1 + 0.05, " Organ") for k in range(600)]
        words = _song(0, 1200) + _speech(1200, 1800) + organ + _speech(1860, 3600) + _song(3600, 4200)
        start, end = hl.locate_sermon(sorted(words, key=lambda w: w.start))
        self.assertEqual(start, 1199.0)
        self.assertAlmostEqual(end, 3600, delta=1.0)

    def test_no_sermon_in_a_concert(self) -> None:
        self.assertIsNone(hl.locate_sermon(self._lyrics(0, 3600)))
        self.assertIsNone(hl.locate_sermon(_song(0, 600) + _speech(600, 780) + _song(780, 3600)))

    def test_find_sermon_raises_friendly_error(self) -> None:
        with mock.patch.object(hl, "transcribe_service", return_value=_song(0, 3600)):
            with self.assertRaisesRegex(RuntimeError, "Couldn't find a clear sermon"):
                hl.find_sermon(Path("service.mp4"))


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
        # Whole seconds, as close to the neighbouring sentences (0.3 s apart here) as allowed.
        self.assertEqual(first.start, hl.whole_second_start(sents[2].start, sents[1].end + 0.02))
        self.assertEqual(first.end, hl.whole_second_end(sents[4].end, sents[5].start - 0.02))
        self.assertTrue(all(h.start.is_integer() and h.end.is_integer() for h in plan.highlights))
        self.assertLessEqual(plan.total_s, hl.MAX_TOTAL_S)
        self.assertEqual(plan.verse_reference, "Genesis 50:20")

    def test_long_highlight_is_shortened_from_the_end(self) -> None:
        sents = _sents([10] * 20)
        plan = build_plan({"highlights": [{"start_sentence": 1, "end_sentence": 12, "title": "t", "reason": "r"}]}, sents)
        self.assertLessEqual(plan.highlights[0].duration, hl.MAX_CLIP_S + 2)
        self.assertEqual(plan.highlights[0].start, hl.whole_second_start(sents[0].start))

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
        self.assertEqual(h.start, hl.whole_second_start(sents[1].start, sents[0].end + 0.02))
        self.assertEqual(h.end, hl.whole_second_end(sents[3].end, sents[4].start - 0.02))

    def test_never_more_than_three(self) -> None:
        sents = _sents([5] * 60)
        picks = [{"start_sentence": a, "end_sentence": a + 3, "title": f"h{a}", "reason": "r"} for a in (1, 10, 20, 30)]
        self.assertLessEqual(len(build_plan({"highlights": picks}, sents).highlights), 3)


class _FakeClaude:
    def __init__(self, payload: dict, stop_reason: str = "end_turn") -> None:
        self.calls: list[dict] = []
        text = SimpleNamespace(type="text", text=json.dumps(payload))
        usage = SimpleNamespace(input_tokens=34_701, output_tokens=1_463,
                                cache_creation_input_tokens=0, cache_read_input_tokens=0)
        response = SimpleNamespace(stop_reason=stop_reason, content=[text] if stop_reason == "end_turn" else [],
                                   model="claude-opus-5-5", usage=usage)
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
        data, usage = hl.ask_claude(_sents([10] * 5), fake)
        self.assertEqual(data["verse"]["reference"], "John 3:16")
        self.assertEqual((usage.input_tokens, usage.output_tokens), (34_701, 1_463))
        self.assertAlmostEqual(usage.cost_usd, 34_701 * 4 / 1e6 + 1_463 * 20 / 1e6)
        self.assertEqual(usage.describe(), "34,701 input + 1,463 output tokens · $0.168")
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
        self.assertIn("$0.168", str(ctx.exception))  # a refusal still costs tokens

    def test_fallback_model_without_a_price_shows_tokens_only(self) -> None:
        response = SimpleNamespace(model="claude-some-fallback", usage=SimpleNamespace(input_tokens=1000, output_tokens=10))
        usage = hl.LuxUsage.from_response(response)
        self.assertIsNone(usage.cost_usd)
        self.assertEqual(usage.describe(), "1,000 input + 10 output tokens · cost unknown for claude-some-fallback")


class TestLuxLedger(unittest.TestCase):
    def test_every_answer_is_logged_and_totalled_even_a_refusal(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "lux_usage.jsonl"
            self.assertEqual(hl.usage_totals(ledger).describe(), "Lux spending so far: $0.00")
            hl.ask_claude(_sents([10] * 5), _FakeClaude(TestAskClaude.payload), ledger=ledger)
            with self.assertRaises(RuntimeError):
                hl.ask_claude(_sents([10] * 5), _FakeClaude(TestAskClaude.payload, stop_reason="refusal"), ledger=ledger)
            totals = hl.usage_totals(ledger)
            self.assertEqual((totals.runs, totals.input_tokens, totals.output_tokens), (2, 69_402, 2_926))
            self.assertEqual(
                totals.describe(), "Lux spending so far: $0.34 · 2 runs · 69,402 input + 2,926 output tokens"
            )

    def test_no_ledger_no_file(self) -> None:
        with mock.patch.object(hl, "record_usage") as record:
            hl.ask_claude(_sents([10] * 5), _FakeClaude(TestAskClaude.payload))
        record.assert_not_called()


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
        self.assertAlmostEqual(plan.sermon_start, 1200.0, delta=1.0)
        self.assertAlmostEqual(plan.sermon_end, 3600.0, delta=1.0)
        self.assertEqual([h.title for h in plan.highlights], ["one", "two"])
        self.assertEqual(plan.usage.output_tokens, 1_463)
        self.assertEqual(plan.highlights[0].start, hl.whole_second_start(sermon[9].start, sermon[8].end + 0.02))
        self.assertEqual(steps, sorted(steps))

    def test_format_timecode_for_wizard(self) -> None:
        self.assertEqual(hl.format_timecode(3000.0), "00:50:00")
        self.assertEqual(hl.format_timecode(4323.754), "01:12:04")

    def test_whole_second_cuts_stay_in_the_silence(self) -> None:
        # Room in the gap: a whole second with a little lead-in / tail.
        self.assertEqual(hl.whole_second_start(100.4, 98.0), 100.0)
        self.assertEqual(hl.whole_second_start(100.1, 98.0), 99.0)
        self.assertEqual(hl.whole_second_end(130.2, 133.0), 131.0)
        self.assertEqual(hl.whole_second_end(130.8, 133.0), 132.0)
        # Gap shorter than a second: overlap whichever side least.
        self.assertEqual(hl.whole_second_start(100.9, 100.7), 101.0)  # clips 0.1 s instead of 0.7 s of the last word
        self.assertEqual(hl.whole_second_start(100.2, 100.1), 100.0)
        self.assertEqual(hl.whole_second_end(130.2, 130.4), 130.0)
        self.assertEqual(hl.whole_second_end(130.8, 130.9), 131.0)



class TestTrimmedScan(unittest.TestCase):
    def test_only_chunks_touched_by_the_trim_are_transcribed_and_cached(self) -> None:
        from tempfile import TemporaryDirectory

        heard: list[float] = []

        def fake_ffmpeg(cmd, **_kw):
            heard.append(float(cmd[cmd.index("-ss") + 1]))
            Path(cmd[-1]).write_bytes(b"wav")
            return SimpleNamespace(returncode=0)

        def fake_whisper(audio, model=None):
            return [Word(5.0, 5.5, " hello"), Word(400.0, 400.5, " world.")]

        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "service.mp4"
            video.write_bytes(b"x")
            with mock.patch.object(hl, "TRANSCRIPT_CACHE", Path(tmp) / "cache"), mock.patch.object(
                hl, "_media_duration", return_value=7200.0
            ), mock.patch.object(hl.subprocess, "run", side_effect=fake_ffmpeg), mock.patch.object(
                hl, "transcribe_words", side_effect=fake_whisper
            ):
                words = hl.transcribe_service(video, ranges=[(2500.0, 3700.0)])
                self.assertEqual(heard, [2400.0, 3000.0, 3600.0])  # chunks 4, 5 and 6 only
                self.assertTrue(all(2500.0 <= w.start < 3700.0 for w in words))
                heard.clear()
                hl.transcribe_service(video, ranges=[(3000.0, 3500.0)])
                self.assertEqual(heard, [])  # already cached

    def test_trimmed_ranges_replace_sermon_detection(self) -> None:
        words = sorted(_song(0, 1200) + _speech(1200, 3600) + _speech(4000, 4300), key=lambda w: w.start)
        payload = {
            "sermon_title": "t",
            "highlights": [{"start_sentence": 2, "end_sentence": 6, "title": "one", "reason": "r"}],
            "verse": {"reference": "Psalm 23:1", "text": "...", "reason": "r"},
        }
        fake = _FakeClaude(payload)
        with mock.patch.object(hl, "transcribe_service", return_value=words) as transcribe, mock.patch.object(
            hl, "find_sermon_window"
        ) as detect:
            plan = hl.find_highlights(Path("service.mp4"), ranges=[(4000.0, 4300.0), (2000.0, 2300.0)], claude=fake)
        detect.assert_not_called()
        self.assertEqual(transcribe.call_args.kwargs["ranges"], [(4000.0, 4300.0), (2000.0, 2300.0)])
        sent = fake.calls[0]["messages"][0]["content"]
        self.assertIn("S1 [0:33:20", sent)  # numbering starts in the earliest trimmed part
        self.assertNotIn("[0:20:", sent)  # nothing outside the trims
        self.assertEqual((plan.sermon_start, plan.sermon_end), (2000.0, 4300.0))
        self.assertTrue(2000.0 <= plan.highlights[0].start < 2300.0)

if __name__ == "__main__":
    unittest.main()
