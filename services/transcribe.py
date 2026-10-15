"""Whisper speech-to-text → short timed captions for the Resolve export."""

from __future__ import annotations

import json
import math
import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from utils.paths import ASSETS

ProgressCb = Optional[Callable[[float, str], None]]

# large-v3-turbo handles mixed Tagalog/English sermons far better than small
# ("sa Diyos" vs "sa Jus") at ~4-5x real time on Apple Silicon.
DEFAULT_MODEL = "large-v3-turbo"
REPLACEMENTS_PATH = ASSETS / "caption_replacements.json"

# Chunking limits for short vertical-video captions.
MAX_WORDS = 5
MAX_CAPTION_S = 3.0
SPLIT_GAP_S = 0.6
MIN_CAPTION_S = 0.4

_SENTENCE_END = (".", "?", "!")
_TOKEN_STRIP_RE = re.compile(r"[^\w']+")

_mlx_repos = {
    "tiny": "mlx-community/whisper-tiny",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}
_faster_models: dict[str, object] = {}


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Caption:
    start: float
    end: float
    text: str
    # Whisper word timings inside this caption (used for word-by-word highlight).
    words: tuple[Word, ...] = field(default=(), compare=False)


def word_timings(caption: Caption) -> list[tuple[str, float, float]]:
    """Display tokens of *caption* with start/end times, contiguous over the caption.

    Uses Whisper's word timings when they still line up with the (possibly
    edited) caption text; a merged word such as "1 Corinthians" shares its span.
    Otherwise — e.g. after the text was rewritten — the caption's span is split
    across tokens in proportion to their length.
    """
    tokens = caption.text.split()
    if not tokens:
        return []
    spans: list[tuple[float, float]] = []
    if caption.words and sum(len(w.text.split()) for w in caption.words) == len(tokens):
        for w in caption.words:
            parts = len(w.text.split())
            step = (w.end - w.start) / parts
            spans.extend((w.start + step * k, w.start + step * (k + 1)) for k in range(parts))
    else:
        total = sum(len(t) for t in tokens)
        t = caption.start
        for token in tokens:
            span = (caption.end - caption.start) * len(token) / total
            spans.append((t, t + span))
            t += span
    # Each token runs until the next one starts so the highlight never blinks off.
    timed: list[tuple[str, float, float]] = []
    for i, token in enumerate(tokens):
        start = caption.start if i == 0 else max(caption.start, min(spans[i][0], caption.end))
        end = caption.end if i + 1 == len(tokens) else max(start, min(spans[i + 1][0], caption.end))
        timed.append((token, start, end))
    return timed


def _progress(cb: ProgressCb, ratio: float, message: str) -> None:
    if cb is not None:
        cb(ratio, message)


def whisper_model_name() -> str:
    return os.environ.get("WORDLY_WHISPER_MODEL", "").strip() or DEFAULT_MODEL


# Sermons are Taglish. Whisper guesses one language from the first 30 s and, if it
# picks English, *translates* the Tagalog that follows; forcing Tagalog keeps both
# languages as spoken. WORDLY_WHISPER_LANGUAGE=auto restores auto-detection.
DEFAULT_LANGUAGE = "tl"


def whisper_language() -> str | None:
    value = os.environ.get("WORDLY_WHISPER_LANGUAGE", "").strip() or DEFAULT_LANGUAGE
    return None if value.lower() == "auto" else value


def _use_mlx() -> bool:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    try:
        import mlx_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def whisper_backend_label() -> str | None:
    """Name of the installed Whisper backend, or None when none is available."""
    if _use_mlx():
        return "mlx-whisper"
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return None
    return "faster-whisper"


def _transcribe_mlx(path: Path, model: str, language: str | None) -> list[Word]:
    import mlx_whisper

    repo = _mlx_repos.get(model, model)
    result = mlx_whisper.transcribe(
        str(path),
        path_or_hf_repo=repo,
        word_timestamps=True,
        language=language,
        verbose=None,
    )
    words: list[Word] = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []):
            words.append(Word(float(w["start"]), float(w["end"]), str(w["word"])))
    return words


def _transcribe_faster(
    path: Path,
    model: str,
    language: str | None,
    progress: Callable[[float], None],
) -> list[Word]:
    from faster_whisper import WhisperModel

    whisper = _faster_models.get(model)
    if whisper is None:
        whisper = WhisperModel(model, device="auto", compute_type="int8")
        _faster_models[model] = whisper

    segments, info = whisper.transcribe(  # type: ignore[attr-defined]
        str(path),
        language=language,
        word_timestamps=True,
        vad_filter=True,
    )
    total = float(getattr(info, "duration", 0.0) or 0.0)
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            words.append(Word(float(w.start), float(w.end), str(w.word)))
        if total > 0:
            progress(min(1.0, float(seg.end) / total))
    return words


def transcribe_words(
    path: Path,
    *,
    progress: Callable[[float], None] | None = None,
    model: str | None = None,
) -> list[Word]:
    """Word-level transcript of *path* (any audio/video ffmpeg can read).

    *model* overrides the configured Whisper model (e.g. a fast pass over a
    whole service).
    """
    model = model or whisper_model_name()
    language = whisper_language()
    if _use_mlx():
        return _transcribe_mlx(path, model, language)
    try:
        import faster_whisper  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Auto captions need Whisper. Install it with:\n"
            "  pip install mlx-whisper   (Apple Silicon Mac)\n"
            "  pip install faster-whisper   (any platform)"
        ) from exc
    return _transcribe_faster(path, model, language, progress or (lambda _r: None))


# --- Term fixes -------------------------------------------------------------


def _normalize_token(text: str) -> str:
    return _TOKEN_STRIP_RE.sub("", text.lower())


def load_replacements(path: Path | None = None) -> dict[tuple[str, ...], str]:
    """Phrase → replacement map from assets/caption_replacements.json (editable)."""
    source = path or REPLACEMENTS_PATH
    if not source.is_file():
        return {}
    raw = json.loads(source.read_text(encoding="utf-8"))
    table: dict[tuple[str, ...], str] = {}
    for phrase, replacement in raw.items():
        if phrase.startswith("_"):
            continue
        replacement = str(replacement)
        for text in (phrase, replacement):
            # The written form maps to itself too, so "1 Corinthians" from Whisper
            # is merged into one word and never split across two captions.
            key = tuple(_normalize_token(t) for t in text.split())
            if key and all(key):
                table.setdefault(key, replacement)
    return table


def apply_replacements(words: list[Word], table: dict[tuple[str, ...], str]) -> list[Word]:
    """Replace spoken phrases (e.g. "first corinthians") with their written form.

    A phrase only matches when no word inside it ends with punctuation, so
    "first, Peter said" is left alone. Trailing punctuation of the last matched
    word is kept.
    """
    if not table:
        return list(words)
    max_len = max(len(k) for k in table)
    out: list[Word] = []
    i = 0
    while i < len(words):
        matched = False
        for n in range(min(max_len, len(words) - i), 0, -1):
            span = words[i : i + n]
            key = tuple(_normalize_token(w.text) for w in span)
            replacement = table.get(key)
            if replacement is None:
                continue
            if any(w.text.strip()[-1:] in ",.;:?!" for w in span[:-1]):
                continue
            last = span[-1].text.strip()
            tail = last[len(last.rstrip(",.;:?!")) :]
            lead = " " if span[0].text.startswith(" ") else ""
            out.append(Word(span[0].start, span[-1].end, f"{lead}{replacement}{tail}"))
            i += n
            matched = True
            break
        if not matched:
            out.append(words[i])
            i += 1
    return out


# --- Chunking ---------------------------------------------------------------


def _caption_text(words: list[Word]) -> str:
    text = "".join(w.text for w in words).strip()
    text = re.sub(r"\s+", " ", text)
    # Social-style captions drop trailing commas/periods but keep ? and !.
    return text.rstrip(",.;:")


def _split_evenly(phrase: list[Word], parts: int) -> list[list[Word]]:
    size = math.ceil(len(phrase) / parts)
    # Recompute so the chunks are balanced (7 words → 4 + 3, not 5 + 2).
    parts = math.ceil(len(phrase) / size)
    base, extra = divmod(len(phrase), parts)
    chunks: list[list[Word]] = []
    i = 0
    for n in range(parts):
        take = base + (1 if n < extra else 0)
        chunks.append(phrase[i : i + take])
        i += take
    return chunks


def chunk_words(
    words: list[Word],
    *,
    max_words: int = MAX_WORDS,
    max_duration_s: float = MAX_CAPTION_S,
    split_gap_s: float = SPLIT_GAP_S,
) -> list[Caption]:
    """Group words into short captions.

    Words are first split into phrases at pauses and sentence ends, then each
    phrase that is too long (words or seconds) is cut into evenly sized chunks
    so no caption is left holding a single stray word.
    """
    phrases: list[list[Word]] = []
    current: list[Word] = []
    for w in words:
        if not w.text.strip():
            continue
        if current:
            paused = w.start - current[-1].end > split_gap_s
            sentence_done = current[-1].text.strip().endswith(_SENTENCE_END)
            if paused or sentence_done:
                phrases.append(current)
                current = []
        current.append(w)
    if current:
        phrases.append(current)

    groups: list[list[Word]] = []
    for phrase in phrases:
        duration = phrase[-1].end - phrase[0].start
        parts = max(
            math.ceil(len(phrase) / max_words),
            math.ceil(duration / max_duration_s) if max_duration_s > 0 else 1,
            1,
        )
        parts = min(parts, len(phrase))
        groups.extend(_split_evenly(phrase, parts) if parts > 1 else [phrase])

    captions: list[Caption] = []
    for idx, group in enumerate(groups):
        start = group[0].start
        end = max(group[-1].end, start + MIN_CAPTION_S)
        if idx + 1 < len(groups):
            end = min(end, groups[idx + 1][0].start)
        text = _caption_text(group)
        if text and end > start:
            captions.append(Caption(start, end, text, tuple(group)))
    return captions


def captions_for_clip(
    path: Path,
    *,
    duration_s: float,
    replacements: dict[tuple[str, ...], str] | None = None,
    progress: Callable[[float], None] | None = None,
    max_words: int = MAX_WORDS,
) -> list[Caption]:
    """Transcribe one clip; caption times are relative to the clip's first frame."""
    words = transcribe_words(path, progress=progress)
    words = apply_replacements(words, replacements if replacements is not None else load_replacements())
    captions: list[Caption] = []
    for cap in chunk_words(words, max_words=max_words):
        if cap.start >= duration_s:
            continue
        end = min(cap.end, duration_s)
        if end > cap.start:
            captions.append(Caption(cap.start, end, cap.text, cap.words))
    return captions


def transcribe_clips(
    clip_paths: list[Path],
    durations_s: list[float],
    *,
    progress_cb: ProgressCb = None,
    ratio_range: tuple[float, float] = (0.0, 1.0),
    should_cancel: Callable[[], bool] | None = None,
    max_words: int = MAX_WORDS,
) -> list[list[Caption]]:
    """Captions per clip, reporting progress inside *ratio_range* of the caller's bar."""
    lo, hi = ratio_range
    replacements = load_replacements()
    backend = whisper_backend_label() or "Whisper"
    count = max(1, len(clip_paths))
    results: list[list[Caption]] = []
    for idx, (clip, dur) in enumerate(zip(clip_paths, durations_s)):
        if should_cancel is not None and should_cancel():
            raise RuntimeError("Cancelled")
        base = lo + (hi - lo) * idx / count
        span = (hi - lo) / count
        label = f"Transcribing {clip.name} ({idx + 1}/{len(clip_paths)}) with {backend}…"
        _progress(progress_cb, base, label)

        def _clip_progress(r: float, base: float = base, span: float = span, label: str = label) -> None:
            _progress(progress_cb, base + span * r, label)

        results.append(
            captions_for_clip(
                clip, duration_s=dur, replacements=replacements, progress=_clip_progress, max_words=max_words
            )
        )
    _progress(progress_cb, hi, "Captions ready")
    return results
