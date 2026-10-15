"""Pick a sermon's highlights automatically: Whisper finds the words, Claude picks the moments.

1. Transcribe the whole service with a fast Whisper model (Tagalog mode keeps
   Taglish as spoken), in chunks so progress and cancel work; cached per video.
2. Locate the sermon: the longest stretch of dense, real speech (worship songs
   come out as sparse or repeated "Amen. Amen." text).
3. Number the sermon's sentences with their start time and length, and ask
   Claude Opus 5.5 for 2-3 self-contained highlights (sentence ranges) plus the
   Bible verse the sermon is built on.
4. Validate in code: 2-3 clips, 12-60 s each, no overlaps, at most 120 s total.
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


def _cache_path(src: Path) -> Path:
    stat = src.stat()
    key = hashlib.sha1(f"{src.resolve()}|{stat.st_size}|{int(stat.st_mtime)}".encode()).hexdigest()[:16]
    return TRANSCRIPT_CACHE / f"{key}.json"


def _media_duration(src: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return float(out)


def transcribe_service(src: Path, *, progress_cb: ProgressCb = None, should_cancel: ShouldCancel = None) -> list[Word]:
    """Word-level transcript of a whole service (cached per video file)."""
    cache = _cache_path(src)
    if cache.is_file():
        _progress(progress_cb, 0.6, "Using saved transcript…")
        return [Word(*w) for w in json.loads(cache.read_text(encoding="utf-8"))]

    duration = _media_duration(src)
    chunks = max(1, math.ceil(duration / CHUNK_S))
    words: list[Word] = []
    with tempfile.TemporaryDirectory(prefix="wordly_service_") as tmp:
        for i in range(chunks):
            if should_cancel is not None and should_cancel():
                raise RuntimeError("Cancelled")
            offset = i * CHUNK_S
            _progress(
                progress_cb,
                0.6 * i / chunks,
                f"Listening to the service — {format_clock(offset)} of {format_clock(duration)}…",
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
            for w in transcribe_words(audio, model=SERVICE_WHISPER_MODEL):
                words.append(Word(w.start + offset, w.end + offset, w.text))
    TRANSCRIPT_CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps([[w.start, w.end, w.text] for w in words]), encoding="utf-8")
    return words


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
    """Free (no Claude): transcribe the service and return the sermon's start/end in seconds."""
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


def transcript_lines(sents: list[Sentence]) -> str:
    return "\n".join(f"S{s.index} [{format_clock(s.start)} +{s.end - s.start:.0f}s] {s.text}" for s in sents)


SYSTEM_PROMPT = """You edit short vertical highlight reels for a Filipino church's Facebook page.

You get the transcript of one sermon, preached in Taglish (Tagalog mixed with English). It was transcribed automatically, so some words are wrong; read past the mistakes to what the preacher meant. Each line is one sentence: its number, its start time, its length in seconds, then the text.

Choose 2 or 3 highlights. They will be cut out and played back to back as one reel, so together they must last at most 120 seconds; use the sentence lengths to stay under it. Each highlight is a continuous range of sentences (start_sentence to end_sentence) lasting roughly 20 to 50 seconds. A viewer scrolling past has not heard the rest of the sermon, so each highlight must make sense on its own: start where a thought begins and end on a strong, complete line.

Look for the sermon's core message, a vivid story or illustration, and a direct challenge or encouragement to the listener. Leave out greetings, announcements, prayers, long Bible readings, jokes that need earlier context, references to things on screen, and passages that are mostly transcription noise. Take the highlights from different parts of the sermon and list them in the order they happen. Give each a short title (2 to 5 words, in the sermon's language) and a one-sentence reason.

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


def ask_claude(sents: list[Sentence], claude=None) -> dict:
    """Send the numbered sermon to Claude Opus 5.5 and return its parsed JSON choice."""
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
            messages=[{"role": "user", "content": "Sermon transcript:\n\n" + transcript_lines(sents)}],
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
        raise RuntimeError("This API key can't use Claude Opus 5.5 (permission denied).") from exc
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to pick highlights for this sermon. Choose timestamps manually.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off. Try again.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)


# --- 4. Validate --------------------------------------------------------------------


def _is_filler(sentence: Sentence) -> bool:
    return len(sentence.text.split()) <= 2 and sentence.end - sentence.start < 2.0


def build_plan(data: dict, sents: list[Sentence]) -> HighlightPlan:
    """Turn Claude's sentence ranges into clean, rule-abiding cuts."""
    by_index = {s.index: s for s in sents}
    notes: list[str] = []
    picked: list[Highlight] = []
    for raw in data.get("highlights", [])[:MAX_CLIPS]:
        first, last = int(raw.get("start_sentence", 0)), int(raw.get("end_sentence", 0))
        if first not in by_index or last not in by_index or last < first:
            notes.append(f"Skipped an invalid suggestion (S{first}–S{last}).")
            continue
        chosen = [by_index[i] for i in range(first, last + 1) if i in by_index]
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


def find_highlights(
    src: Path,
    *,
    progress_cb: ProgressCb = None,
    should_cancel: ShouldCancel = None,
    claude=None,
) -> HighlightPlan:
    """The whole pipeline for one service video."""
    words = transcribe_service(src, progress_cb=progress_cb, should_cancel=should_cancel)
    _progress(progress_cb, 0.65, "Finding the sermon…")
    start, end = locate_sermon(words) or find_sermon_window(words)
    sents = sentences(words, start, end)
    if len(sents) < 10:
        raise RuntimeError("Couldn't find enough speech to pick highlights from. Choose timestamps manually.")
    if should_cancel is not None and should_cancel():
        raise RuntimeError("Cancelled")
    _progress(
        progress_cb,
        0.7,
        f"Asking Claude to pick highlights from the sermon ({format_clock(start)}–{format_clock(end)})…",
    )
    plan = build_plan(ask_claude(sents, claude), sents)
    plan.sermon_start, plan.sermon_end = start, end
    if not plan.highlights:
        raise RuntimeError("Claude's suggestions didn't fit the reel rules. Choose timestamps manually.")
    _progress(progress_cb, 1.0, f"Found {len(plan.highlights)} highlights ({plan.total_s:.0f} s)")
    return plan
