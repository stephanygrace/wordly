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
    """HH:MM:SS.ss for the wizard's segment fields."""
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:05.2f}"


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
        start = max(0.0, chosen[0].start - LEAD_IN_S, before.end + 0.02 if before else 0.0)
        end = min(chosen[-1].end + TAIL_S, after.start - 0.02 if after else float("inf"))
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
        if len(inside) > 1 and inside[-1].start - 0.02 - h.start >= MIN_CLIP_S:
            # End just before the dropped sentence (with breathing room up to its start).
            new_end = min(inside[-2].end + TAIL_S, inside[-1].start - 0.02)
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
    start, end = find_sermon_window(words)
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
