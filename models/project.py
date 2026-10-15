from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from utils.export_name import default_export_project_name

if TYPE_CHECKING:
    from services.transcribe import Caption


@dataclass
class ClipSegment:
    """One highlight window inside the source sermon."""

    start_text: str
    end_text: str
    label: str = ""

    @property
    def display_name(self) -> str:
        if self.label.strip():
            return self.label.strip()
        return f"{self.start_text} → {self.end_text}"


@dataclass
class VerseChoice:
    reference: str
    text: str


@dataclass
class MusicChoice:
    title: str
    artist: str = ""
    search_query: str = ""
    local_path: Optional[Path] = None

    @property
    def display_name(self) -> str:
        if self.artist:
            return f"{self.title} — {self.artist}"
        return self.title


@dataclass
class ProjectState:
    """Mutable wizard state carried across all steps."""

    fb_url: str = ""
    cookies_file: Optional[Path] = None
    sermon_path: Optional[Path] = None
    sermon_duration_s: float = 0.0
    segments: list[ClipSegment] = field(default_factory=list)
    # Individual exported clips (Clip001.mp4, Clip002.mp4, …) — primary storage.
    clip_paths: list[Path] = field(default_factory=list)
    # Optional single-clip fallback when only one reel is produced.
    joined_clip_path: Optional[Path] = None
    theme: str = ""
    verse_choices: list[VerseChoice] = field(default_factory=list)
    selected_verse: Optional[VerseChoice] = None
    music_choices: list[MusicChoice] = field(default_factory=list)
    selected_music: Optional[MusicChoice] = None
    project_name: str = field(default_factory=default_export_project_name)
    resolve_output_path: Optional[Path] = None
    # Transcribe clips with Whisper and add caption titles to the Resolve timeline.
    auto_captions: bool = False
    # Reviewed captions per clip (times relative to each clip), from the Captions step.
    clip_captions: list[list["Caption"]] = field(default_factory=list)
    # Clip files the captions were made from; captions are stale if clip_paths changes.
    captions_source: list[Path] = field(default_factory=list)
    # Caption look (name from assets/caption_styles.json); None = default style.
    caption_style: Optional[str] = None
    # Sermon-reel layout: blurred background, video centred, verse block above it.
    reel_layout: bool = True

    def captions_current(self) -> bool:
        return bool(self.clip_captions) and self.captions_source == list(self.clip_paths)
