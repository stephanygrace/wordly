"""Pick a sermon's highlights automatically: Whisper finds the words, Lux picks the moments.

Lux is Wordly's name for the assistant; it runs on Claude Opus 5.5.

1. Transcribe the part to scan with a fast Whisper model (Tagalog mode keeps
   Taglish as spoken), in 10-minute chunks so progress and cancel work; each
   chunk is cached, so nothing is transcribed twice.
2. Scan range: the ranges the user trimmed, or else the sermon located
   automatically as the longest stretch of dense, real speech (worship songs
   come out as sparse or repeated "Amen. Amen." text).
3. Watch where the speaker looks (services.screen_gaze) to find where they
   read from the off-camera screen.
4. Number the sentences with their start time and length, mark long silences
   and screen reading, and ask Claude for 4-5 ranked, self-contained highlights
   (sentence ranges) plus the Bible verse the sermon is built on.
5. Validate in code: the first 2-3 good clips, 12-60 s each, no 3 s silence or
   screen reading inside, no overlaps, at most 120 s total.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from services.screen_gaze import find_screen_reading
from services.transcribe import Word, transcribe_words
from utils.paths import TEMP

ProgressCb = Optional[Callable[[float, str], None]]
ShouldCancel = Optional[Callable[[], bool]]

CLAUDE_MODEL = "claude-opus-5-5"
# Fast pass over a 2-hour service: ~7 min on an M3, and Tagalog mode keeps Taglish.
SERVICE_WHISPER_MODEL = "small"
CHUNK_S = 600
TRANSCRIPT_CACHE = TEMP / "transcripts"

MAX_TOTAL_S = 120.0
MIN_CLIP_S = 12.0
MAX_CLIP_S = 60.0
MAX_CLIPS = 3
# A silence this long inside a clip reads as dead air; a clip never spans one.
MAX_IDLE_S = 3.0
# Breathing room around each cut.
LEAD_IN_S = 0.15
TAIL_S = 0.35


@dataclass(frozen=True)
class Sentence:
    index: int
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Highlight:
    start: float
    end: float
    title: str
    reason: str

    @property
    def duration(self) -> float:
        return self.end - self.start


# USD per million tokens: input, output, cache write (5 min), cache read.
PRICES_PER_MTOK = {
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
}


@dataclass
class LuxUsage:
    """Tokens one Lux request used, and what they cost."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def from_response(cls, response) -> "LuxUsage":
        usage = getattr(response, "usage", None)
        tokens = lambda name: int(getattr(usage, name, 0) or 0)  # noqa: E731
        return cls(
            model=str(getattr(response, "model", "") or CLAUDE_MODEL),
            input_tokens=tokens("input_tokens"),
            output_tokens=tokens("output_tokens"),
            cache_write_tokens=tokens("cache_creation_input_tokens"),
            cache_read_tokens=tokens("cache_read_input_tokens"),
        )

    @property
    def cost_usd(self) -> float | None:
        prices = PRICES_PER_MTOK.get(self.model)
        if prices is None:
            return None
        counts = (self.input_tokens, self.output_tokens, self.cache_write_tokens, self.cache_read_tokens)
        return sum(n * price for n, price in zip(counts, prices)) / 1_000_000

    def describe(self) -> str:
        """e.g. "34,701 input + 1,463 output tokens · $0.168"."""
        text = f"{self.input_tokens + self.cache_write_tokens + self.cache_read_tokens:,} input + {self.output_tokens:,} output tokens"
        cost = self.cost_usd
        text += f" · ${cost:.3f}" if cost is not None else f" · cost unknown for {self.model}"
        return text


# Every Lux request's tokens and cost, one JSON line each (temp/ is git-ignored).
LUX_LEDGER = TEMP / "lux_usage.jsonl"


@dataclass
class LuxTotals:
    runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def describe(self) -> str:
        if not self.runs:
            return "Lux spending so far: $0.00"
        runs = "1 run" if self.runs == 1 else f"{self.runs} runs"
        return (
            f"Lux spending so far: ${self.cost_usd:.2f} · {runs} · "
            f"{self.input_tokens:,} input + {self.output_tokens:,} output tokens"
        )


def record_usage(usage: LuxUsage, ledger: Path) -> None:
    import time

    ledger.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": usage.model,
        "input_tokens": usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": usage.cost_usd,
    }
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def usage_totals(ledger: Path | None = None) -> LuxTotals:
    ledger = ledger or LUX_LEDGER
    totals = LuxTotals()
    if not ledger.is_file():
        return totals
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        totals.runs += 1
        totals.input_tokens += int(row.get("input_tokens") or 0)
        totals.output_tokens += int(row.get("output_tokens") or 0)
        totals.cost_usd += float(row.get("cost_usd") or 0.0)
    return totals


@dataclass
class HighlightPlan:
    highlights: list[Highlight]
    verse_reference: str = ""
    verse_text: str = ""
    verse_reason: str = ""
    sermon_title: str = ""
    sermon_start: float = 0.0
    sermon_end: float = 0.0
    notes: list[str] = field(default_factory=list)
    usage: LuxUsage | None = None

    @property
    def total_s(self) -> float:
        return sum(h.duration for h in self.highlights)


def _progress(cb: ProgressCb, ratio: float, message: str) -> None:
    if cb is not None:
        cb(ratio, message)


def format_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def format_timecode(seconds: float) -> str:
    """HH:MM:SS for the wizard's segment fields (cuts are whole seconds)."""
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def whole_second_start(first_word: float, earliest: float = 0.0) -> float:
    """Whole-second cut just before *first_word*, not before *earliest* (the previous words) if possible."""
    for t in (math.floor(first_word - LEAD_IN_S), math.floor(first_word)):
        if t >= earliest:
            return float(max(0, t))
    # The gap is under a second: take whichever second overlaps less.
    before, after = math.floor(first_word), math.ceil(first_word)
    return float(max(0, before if earliest - before <= after - first_word else after))


def whole_second_end(last_word_end: float, latest: float = math.inf) -> float:
    """Whole-second cut just after *last_word_end*, not after *latest* (the next words) if possible."""
    for t in (math.ceil(last_word_end + TAIL_S), math.ceil(last_word_end)):
        if t <= latest:
            return float(t)
    after, before = math.ceil(last_word_end), math.floor(last_word_end)
    return float(after if after - latest <= last_word_end - before else before)


# --- 1. Transcript ------------------------------------------------------------


def _cache_key(src: Path) -> str:
    stat = src.stat()
    return hashlib.sha1(f"{src.resolve()}|{stat.st_size}|{int(stat.st_mtime)}".encode()).hexdigest()[:16]


def _cache_path(src: Path) -> Path:
    """Whole-service transcript saved by earlier versions (still used when present)."""
    return TRANSCRIPT_CACHE / f"{_cache_key(src)}.json"


def _chunk_cache_path(src: Path, index: int) -> Path:
    return TRANSCRIPT_CACHE / f"{_cache_key(src)}_{index:03d}.json"


def _media_duration(src: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return float(out)


def _in_ranges(word: Word, ranges: list[tuple[float, float]]) -> bool:
    return any(a <= word.start < b for a, b in ranges)


def transcribe_service(
    src: Path,
    *,
    ranges: list[tuple[float, float]] | None = None,
    progress_cb: ProgressCb = None,
    should_cancel: ShouldCancel = None,
) -> list[Word]:
    """Word-level transcript of *ranges* of the video (default: all of it).

    Only the 10-minute chunks the ranges touch are transcribed, and each chunk is
    cached, so scanning a trimmed part is faster and nothing is heard twice.
    """
    legacy = _cache_path(src)
    if legacy.is_file():
        _progress(progress_cb, 0.6, "Using saved transcript…")
        words = [Word(*w) for w in json.loads(legacy.read_text(encoding="utf-8"))]
        return [w for w in words if ranges is None or _in_ranges(w, ranges)]

    duration = _media_duration(src)
    spans = ranges or [(0.0, duration)]
    needed = sorted(
        {i for a, b in spans for i in range(int(a // CHUNK_S), max(int(a // CHUNK_S) + 1, math.ceil(b / CHUNK_S)))}
    )
    words: list[Word] = []
    TRANSCRIPT_CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wordly_service_") as tmp:
        for k, i in enumerate(needed):
            if should_cancel is not None and should_cancel():
                raise RuntimeError("Cancelled")
            offset = i * CHUNK_S
            cache = _chunk_cache_path(src, i)
            if cache.is_file():
                words.extend(Word(*w) for w in json.loads(cache.read_text(encoding="utf-8")))
                continue
            _progress(
                progress_cb,
                0.6 * k / len(needed),
                f"Listening — {format_clock(offset)} to {format_clock(min(duration, offset + CHUNK_S))} "
                f"(part {k + 1} of {len(needed)})…",
            )
            audio = Path(tmp) / f"chunk_{i:03d}.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{offset}", "-t", f"{CHUNK_S}",
                    "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(audio),
                ],
                check=True,
                capture_output=True,
            )
            chunk = [
                Word(w.start + offset, w.end + offset, w.text)
                for w in transcribe_words(audio, model=SERVICE_WHISPER_MODEL)
            ]
            cache.write_text(json.dumps([[w.start, w.end, w.text] for w in chunk]), encoding="utf-8")
            words.extend(chunk)
    return [w for w in words if _in_ranges(w, spans)]


# --- 2. Where is the sermon? -----------------------------------------------------


def _is_real_speech(words: list[Word], min_words: int) -> bool:
    if len(words) < min_words:
        return False
    tokens = [w.text.strip().lower().strip(".,!?") for w in words if w.text.strip()]
    # Whisper fills songs with repeated phrases ("Amen. Amen. Amen."); speech varies.
    return len(set(tokens)) / max(1, len(tokens)) >= 0.25


def find_sermon_window(
    words: list[Word], *, window_s: float = 300.0, min_words: int = 300
) -> tuple[float, float]:
    """Start/end seconds of the longest run of dense speech (one quiet window allowed)."""
    if not words:
        return 0.0, 0.0
    total = words[-1].end
    count = math.ceil(total / window_s)
    dense = []
    for i in range(count):
        inside = [w for w in words if i * window_s <= w.start < (i + 1) * window_s]
        dense.append(_is_real_speech(inside, min_words))
    best: tuple[int, int] | None = None
    i = 0
    while i < count:
        if not dense[i]:
            i += 1
            continue
        j = i
        while j + 1 < count and (dense[j + 1] or (j + 2 < count and dense[j + 2])):
            j += 1
        if not dense[j]:
            j -= 1
        if best is None or (j - i) > (best[1] - best[0]):
            best = (i, j)
        i = j + 1
    if best is None:
        return 0.0, total
    return best[0] * window_s, min(total, (best[1] + 1) * window_s)


# Precise sermon bounds, for the free "Find sermon" trim: 60 s windows every 10 s.
SERMON_STEP_S = 10.0
SERMON_WINDOW_S = 60.0
SERMON_MIN_WORDS = 70  # per minute, after collapsing repeats; preaching runs ~100-150
SERMON_MIN_VARIETY = 0.45  # unique share of words; song lyrics repeat
SERMON_MIN_S = 300.0
SERMON_PAUSE_S = 4.0
SERMON_EDGE_S = 30.0


def _sounds_spoken(words: list[Word], start: float, end: float) -> bool:
    """True if the words in [start, end) read like talking: several, and not a repeated lyric."""
    tokens = [_token(w) for w in _collapse_repeats([w for w in words if start <= w.start < end])]
    return len(tokens) >= 8 and len(set(tokens)) / len(tokens) >= 0.5


def _phrase_after(words: list[Word], k: int) -> tuple[float, float]:
    """From word k up to the next long pause (at most SERMON_EDGE_S)."""
    j = k
    while j + 1 < len(words) and words[j + 1].start - words[j].end < SERMON_PAUSE_S and words[j + 1].start < words[k].start + SERMON_EDGE_S:
        j += 1
    return words[k].start, words[j].end


def _phrase_before(words: list[Word], k: int) -> tuple[float, float]:
    """Back from word k to the previous long pause (at most SERMON_EDGE_S)."""
    j = k
    while j > 0 and words[j].start - words[j - 1].end < SERMON_PAUSE_S and words[j - 1].start > words[k].end - SERMON_EDGE_S:
        j -= 1
    return words[j].start, words[k].end + 0.001


def _token(word: Word) -> str:
    return word.text.strip().lower().strip(".,!?-'\"")


def _collapse_repeats(words: list[Word]) -> list[Word]:
    """Drop empty words and back-to-back repeats (Whisper hears music as "Organ Organ Organ...")."""
    out: list[Word] = []
    for w in words:
        token = _token(w)
        if token and not (out and _token(out[-1]) == token):
            out.append(w)
    return out


def _preaching_windows(words: list[Word]) -> list[bool]:
    clean = _collapse_repeats(words)
    starts = [w.start for w in clean]
    flags = []
    for i in range(int(words[-1].end // SERMON_STEP_S) + 1):
        t = i * SERMON_STEP_S
        inside = clean[bisect.bisect_left(starts, t) : bisect.bisect_left(starts, t + SERMON_WINDOW_S)]
        tokens = [_token(w) for w in inside]
        flags.append(
            len(tokens) >= SERMON_MIN_WORDS and len(set(tokens)) / max(1, len(tokens)) >= SERMON_MIN_VARIETY
        )
    return flags


def _longest_preaching_run(flags: list[bool]) -> tuple[int, int] | None:
    runs: list[list[int]] = []
    i = 0
    while i < len(flags):
        if not flags[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(flags) and flags[j + 1]:
            j += 1
        gap_s = (i - runs[-1][1] - 1) * SERMON_STEP_S if runs else math.inf
        long_run = (j - i + 1) * SERMON_STEP_S >= 120
        # A short break (a song cue, a garbled minute) doesn't end the sermon if more preaching follows.
        if gap_s <= 30 or (gap_s <= 120 and long_run):
            runs[-1][1] = j
        else:
            runs.append([i, j])
        i = j + 1
    if not runs:
        return None
    first, last = max(runs, key=lambda r: r[1] - r[0])
    return first, last


def locate_sermon(words: list[Word]) -> tuple[float, float] | None:
    """Start/end seconds of the sermon, cut at pauses in speech; None if there's no clear sermon.

    The sermon is the longest stretch where every minute is dense, varied speech;
    the edges snap to the pause where speaking starts and stops.
    """
    words = [w for w in words if w.text.strip()]
    if not words:
        return None
    run = _longest_preaching_run(_preaching_windows(words))
    if run is None:
        return None
    first_t = run[0] * SERMON_STEP_S
    last_t = run[1] * SERMON_STEP_S
    if last_t + SERMON_WINDOW_S - first_t < SERMON_MIN_S:
        return None
    # Edges: the pause where speech (not lyrics) begins, and where it stops.
    reach = SERMON_EDGE_S
    starts = [
        k for k, w in enumerate(words)
        if first_t - reach <= w.start < first_t + SERMON_WINDOW_S
        and (k == 0 or w.start - words[k - 1].end >= SERMON_PAUSE_S)
    ]
    start_k = next(
        (k for k in starts if _sounds_spoken(words, *_phrase_after(words, k))),
        bisect.bisect_left([w.start for w in words], first_t),
    )
    ends = [
        k for k, w in enumerate(words)
        if last_t < w.start < last_t + SERMON_WINDOW_S + reach
        and (k + 1 == len(words) or words[k + 1].start - w.end >= SERMON_PAUSE_S)
    ]
    end_k = next(
        (k for k in reversed(ends) if _sounds_spoken(words, *_phrase_before(words, k))),
        bisect.bisect_left([w.start for w in words], last_t + SERMON_WINDOW_S) - 1,
    )
    start = whole_second_start(words[start_k].start, words[start_k - 1].end + 0.02 if start_k > 0 else 0.0)
    end = whole_second_end(words[end_k].end, words[end_k + 1].start - 0.02 if end_k + 1 < len(words) else math.inf)
    return start, end


def find_sermon(src: Path, *, progress_cb: ProgressCb = None, should_cancel: ShouldCancel = None) -> tuple[float, float]:
    """Free (no Lux): transcribe the service and return the sermon's start/end in seconds."""
    words = transcribe_service(src, progress_cb=progress_cb, should_cancel=should_cancel)
    _progress(progress_cb, 0.9, "Finding the sermon…")
    bounds = locate_sermon(words)
    if bounds is None:
        raise RuntimeError("Couldn't find a clear sermon in this video. Mark the start and end on the player.")
    _progress(progress_cb, 1.0, f"Sermon found: {format_clock(bounds[0])}–{format_clock(bounds[1])}")
    return bounds


# --- 3. Sentences and Claude -------------------------------------------------------


def sentences(words: list[Word], start: float, end: float, *, max_s: float = 20.0, pause_s: float = 0.8) -> list[Sentence]:
    """Group words into numbered sentences (punctuation, pauses, or 20 s max)."""
    out: list[Sentence] = []
    current: list[Word] = []

    def flush() -> None:
        if current:
            text = " ".join("".join(w.text for w in current).split())
            out.append(Sentence(len(out) + 1, current[0].start, current[-1].end, text))
            current.clear()

    for w in words:
        if w.start < start or w.start >= end or not w.text.strip():
            continue
        if current and (w.start - current[-1].end > pause_s or w.end - current[0].start > max_s):
            flush()
        current.append(w)
        if w.text.strip().endswith((".", "?", "!")):
            flush()
    flush()
    return out


def _idle_before(sents: list[Sentence], k: int) -> float:
    """Seconds of silence between sentence k and the one before it."""
    return sents[k].start - sents[k - 1].end if k > 0 else 0.0


def reads_screen(sentence: Sentence, reading: list[tuple[float, float]]) -> bool:
    """True if the speaker is reading from the screen for at least half of *sentence*."""
    overlap = sum(max(0.0, min(b, sentence.end) - max(a, sentence.start)) for a, b in reading)
    return overlap >= 0.5 * max(0.1, sentence.end - sentence.start)


def transcript_lines(sents: list[Sentence], reading: list[tuple[float, float]] = ()) -> str:
    lines = []
    for k, s in enumerate(sents):
        marks = ""
        if _idle_before(sents, k) >= MAX_IDLE_S:
            marks += f"(after {_idle_before(sents, k):.0f} s of silence) "
        if reads_screen(s, reading):
            marks += "(reading the screen) "
        lines.append(f"S{s.index} [{format_clock(s.start)} +{s.end - s.start:.0f}s] {marks}{s.text}")
    return "\n".join(lines)


SYSTEM_PROMPT = """You edit short vertical highlight reels for a Filipino church's Facebook page.

You get the transcript of one sermon, preached in Taglish (Tagalog mixed with English). It was transcribed automatically, so some words are wrong; read past the mistakes to what the preacher meant. Each line is one sentence: its number, its start time, its length in seconds, then the text.

List 4 or 5 candidate highlights, best first. The app keeps the first 2 or 3 that pass its checks and plays them back to back as one reel, so your top three together must last at most 120 seconds; use the sentence lengths to stay under it. The rest are backups. Each highlight is a continuous range of sentences (start_sentence to end_sentence) lasting roughly 20 to 50 seconds. A viewer scrolling past has not heard the rest of the sermon, so each highlight must make sense on its own: start where a thought begins and end on a strong, complete line.

Every highlight must be a lesson: the preacher teaching directly, such as the sermon's core message, a truth explained plainly, or a direct challenge or encouragement to the listener. Do not pick stories, illustrations, testimonies, or retellings of Bible narratives, even vivid ones; if a story leads into a lesson, start the highlight where the lesson begins. Leave out greetings, announcements, prayers, long Bible readings, jokes that need earlier context, references to things on screen, and passages that are mostly transcription noise.

The camera can't see the projector screen. Never pick lines where the preacher reads aloud from it (slides, points, or a verse); they look away from the viewer the whole time. Lines marked (reading the screen) are those moments, seen on the video; never include one in a highlight. Also leave out any passage that reads like a slide or a quoted verse, even unmarked. A line marked (after N s of silence) follows a long pause, which is dead air in a reel: a highlight may start at that line but must never run across the pause.

Take the highlights from different parts of the sermon. Give each a short title (2 to 5 words, in the sermon's language) and a one-sentence reason.

Also choose the one Bible verse that best captures the sermon's message, preferring the passage the preacher is preaching from. Give its reference and its text in English (NIV wording). It will be shown on screen, and a person will check the wording before publishing."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "sermon_title": {"type": "string"},
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_sentence": {"type": "integer"},
                    "end_sentence": {"type": "integer"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["start_sentence", "end_sentence", "title", "reason"],
                "additionalProperties": False,
            },
        },
        "verse": {
            "type": "object",
            "properties": {
                "reference": {"type": "string"},
                "text": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["reference", "text", "reason"],
            "additionalProperties": False,
        },
    },
    "required": ["sermon_title", "highlights", "verse"],
    "additionalProperties": False,
}


def ask_claude(
    sents: list[Sentence],
    claude=None,
    *,
    ledger: Path | None = None,
    reading: list[tuple[float, float]] = (),
) -> tuple[dict, LuxUsage]:
    """Send the numbered sermon to Claude Opus 5.5; return its parsed JSON choice and the tokens used.

    *reading* is where the speaker reads from the screen; those sentences are marked.
    With *ledger*, the tokens are recorded there even if the answer turns out unusable.
    """
    if claude is None:
        from services.claude_client import client

        claude = client()
    import anthropic

    try:
        response = claude.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            # If the request is declined, Anthropic re-runs it on its recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": "high",
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "Sermon transcript:\n\n" + transcript_lines(sents, reading)}],
        )
    except anthropic.AuthenticationError as exc:
        raise RuntimeError("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in the project's .env.") from exc
    except anthropic.BadRequestError as exc:
        if "workspace" in str(exc).lower():
            raise RuntimeError(
                "This API key isn't tied to a workspace. Create the key inside a workspace in the "
                "Anthropic Console, or add ANTHROPIC_WORKSPACE_ID=wrkspc_… to the project's .env."
            ) from exc
        raise
    except anthropic.PermissionDeniedError as exc:
        raise RuntimeError("This API key can't use the model Lux runs on (Claude Opus 5.5).") from exc
    usage = LuxUsage.from_response(response)
    if ledger is not None:
        record_usage(usage, ledger)
    if response.stop_reason == "refusal":
        raise RuntimeError(
            f"Lux declined to pick highlights for this sermon. Choose timestamps manually. (Used {usage.describe()}.)"
        )
    if response.stop_reason == "max_tokens":
        raise RuntimeError(f"Lux's answer was cut off. Try again. (Used {usage.describe()}.)")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text), usage


# --- 4. Validate --------------------------------------------------------------------


def _is_filler(sentence: Sentence) -> bool:
    return len(sentence.text.split()) <= 2 and sentence.end - sentence.start < 2.0


def _clean_runs(chosen: list[Sentence], reading: list[tuple[float, float]]) -> list[list[Sentence]]:
    """*chosen* split at long silences, without the sentences read from the screen."""
    runs: list[list[Sentence]] = [[]]
    for k, s in enumerate(chosen):
        if k > 0 and s.start - chosen[k - 1].end >= MAX_IDLE_S:
            runs.append([])
        if reads_screen(s, reading):
            runs.append([])
        else:
            runs[-1].append(s)
    return [run for run in runs if run]


def build_plan(data: dict, sents: list[Sentence], reading: list[tuple[float, float]] = ()) -> HighlightPlan:
    """Turn Claude's ranked sentence ranges into clean, rule-abiding cuts.

    The first MAX_CLIPS suggestions that survive the checks are kept. A clip never
    spans a silence of MAX_IDLE_S or includes a sentence read from the screen
    (*reading*); it is cut down to its longest clean part instead.
    """
    by_index = {s.index: s for s in sents}
    notes: list[str] = []
    picked: list[Highlight] = []
    for raw in data.get("highlights", []):
        if len(picked) == MAX_CLIPS:
            break
        first, last = int(raw.get("start_sentence", 0)), int(raw.get("end_sentence", 0))
        if first not in by_index or last not in by_index or last < first:
            notes.append(f"Skipped an invalid suggestion (S{first}–S{last}).")
            continue
        chosen = [by_index[i] for i in range(first, last + 1) if i in by_index]
        runs = _clean_runs(chosen, reading)
        if not runs:
            notes.append(f"Skipped “{raw.get('title', '')}” (the speaker reads from the screen).")
            continue
        if len(runs) > 1 or len(runs[0]) < len(chosen):
            why = "screen reading" if any(reads_screen(s, reading) for s in chosen) else "a long pause"
            chosen = max(runs, key=lambda run: run[-1].end - run[0].start)
            notes.append(f"Shortened “{raw.get('title', '')}” to leave out {why}.")
        # Don't open or close on a dangling filler ("So,", "Okay?", "No?").
        while len(chosen) > 1 and _is_filler(chosen[-1]):
            chosen.pop()
        while len(chosen) > 1 and _is_filler(chosen[0]):
            chosen.pop(0)
        # Too long: drop sentences from the end (keeps the opening of the thought).
        while len(chosen) > 1 and chosen[-1].end - chosen[0].start > MAX_CLIP_S:
            chosen.pop()
        # Breathing room, but never into the neighbouring sentences' words.
        before = by_index.get(chosen[0].index - 1)
        after = by_index.get(chosen[-1].index + 1)
        start = whole_second_start(chosen[0].start, before.end + 0.02 if before else 0.0)
        end = whole_second_end(chosen[-1].end, after.start - 0.02 if after else math.inf)
        if end - start < MIN_CLIP_S:
            notes.append(f"Skipped “{raw.get('title', '')}” (too short).")
            continue
        if any(start < h.end and h.start < end for h in picked):
            notes.append(f"Skipped “{raw.get('title', '')}” (overlaps another highlight).")
            continue
        picked.append(Highlight(start, end, str(raw.get("title", "")).strip(), str(raw.get("reason", "")).strip()))

    # Over the reel limit: trim the longest highlight's last sentences, then drop the last pick.
    def over() -> float:
        return sum(h.duration for h in picked) - MAX_TOTAL_S

    while picked and over() > 0:
        longest = max(range(len(picked)), key=lambda k: picked[k].duration)
        h = picked[longest]
        inside = [s for s in sents if h.start <= s.start and s.end <= h.end + 0.01]
        new_end = whole_second_end(inside[-2].end, inside[-1].start - 0.02) if len(inside) > 1 else 0.0
        if len(inside) > 1 and h.start < new_end < h.end and new_end - h.start >= MIN_CLIP_S:
            # End just before the dropped sentence.
            picked[longest] = Highlight(h.start, new_end, h.title, h.reason)
        else:
            notes.append(f"Dropped “{picked[-1].title}” to stay within {MAX_TOTAL_S:.0f} s.")
            picked.pop()
    picked.sort(key=lambda h: h.start)
    verse = data.get("verse") or {}
    return HighlightPlan(
        highlights=picked,
        verse_reference=str(verse.get("reference", "")).strip(),
        verse_text=str(verse.get("text", "")).strip(),
        verse_reason=str(verse.get("reason", "")).strip(),
        sermon_title=str(data.get("sermon_title", "")).strip(),
        notes=notes,
    )


def _screen_reading(
    src: Path,
    spans: list[tuple[float, float]],
    *,
    progress_cb: ProgressCb = None,
    should_cancel: ShouldCancel = None,
) -> tuple[list[tuple[float, float]], str]:
    """Where the speaker reads from the screen, and a note if that couldn't be checked."""
    _progress(progress_cb, 0.66, "Watching where the speaker looks…")
    try:
        reading = find_screen_reading(
            src,
            spans,
            progress_cb=lambda r, m: _progress(progress_cb, 0.66 + 0.04 * r, m),
            should_cancel=should_cancel,
        )
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        if str(exc) == "Cancelled":
            raise
        return [], f"Couldn't check whether the speaker reads from the screen ({exc})."
    return reading, ""


def find_highlights(
    src: Path,
    *,
    ranges: list[tuple[float, float]] | None = None,
    progress_cb: ProgressCb = None,
    should_cancel: ShouldCancel = None,
    claude=None,
    ledger: Path | None = None,
) -> HighlightPlan:
    """Lux's highlights for one service video.

    *ranges* are the parts the user trimmed (seconds); only those are scanned.
    Without them the whole video is transcribed and the sermon found automatically.
    """
    words = transcribe_service(src, ranges=ranges, progress_cb=progress_cb, should_cancel=should_cancel)
    if ranges:
        spans = sorted(ranges)
        sents: list[Sentence] = []
        for a, b in spans:
            for s in sentences(words, a, b):
                sents.append(Sentence(len(sents) + 1, s.start, s.end, s.text))
        start, end = spans[0][0], spans[-1][1]
        where = "your trimmed part" if len(spans) == 1 else f"your {len(spans)} trimmed parts"
    else:
        _progress(progress_cb, 0.65, "Finding the sermon…")
        start, end = locate_sermon(words) or find_sermon_window(words)
        sents = sentences(words, start, end)
        where = "the sermon"
    if len(sents) < 10:
        raise RuntimeError("Couldn't find enough speech to pick highlights from. Choose timestamps manually.")
    reading, gaze_note = _screen_reading(
        src, sorted(ranges) if ranges else [(start, end)], progress_cb=progress_cb, should_cancel=should_cancel
    )
    if should_cancel is not None and should_cancel():
        raise RuntimeError("Cancelled")
    _progress(progress_cb, 0.7, f"Lux is picking highlights from {where} ({format_clock(start)}–{format_clock(end)})…")
    data, usage = ask_claude(sents, claude, ledger=ledger, reading=reading)
    plan = build_plan(data, sents, reading)
    if gaze_note:
        plan.notes.append(gaze_note)
    plan.sermon_start, plan.sermon_end = start, end
    plan.usage = usage
    if not plan.highlights:
        raise RuntimeError(
            f"Lux's suggestions didn't fit the reel rules. Choose timestamps manually. (Used {usage.describe()}.)"
        )
    _progress(progress_cb, 1.0, f"Lux found {len(plan.highlights)} highlights ({plan.total_s:.0f} s) · {usage.describe()}")
    return plan
