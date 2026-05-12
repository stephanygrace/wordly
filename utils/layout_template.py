from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from utils.paths import TEMPLATES


@dataclass(frozen=True)
class ReelLayoutTemplate:
    """Vertical reel geometry and encode hints (from JSON)."""

    name: str
    width: int
    height: int
    top_overlay_px: int
    middle_video_px: int
    bottom_reserved_px: int
    fps: int
    video_crf: int
    overlay_background_alpha: float

    @property
    def middle_video_height(self) -> int:
        return self.middle_video_px

    @property
    def video_overlay_y(self) -> int:
        return self.top_overlay_px


def default_layout() -> ReelLayoutTemplate:
    return ReelLayoutTemplate(
        name="Default 9:16",
        width=1080,
        height=1920,
        top_overlay_px=480,
        middle_video_px=960,
        bottom_reserved_px=480,
        fps=30,
        video_crf=20,
        overlay_background_alpha=0.62,
    )


def load_layout(path: Path) -> ReelLayoutTemplate:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ReelLayoutTemplate(
        name=str(data.get("name", path.stem)),
        width=int(data["width"]),
        height=int(data["height"]),
        top_overlay_px=int(data["top_overlay_px"]),
        middle_video_px=int(data["middle_video_px"]),
        bottom_reserved_px=int(data["bottom_reserved_px"]),
        fps=int(data.get("fps", 30)),
        video_crf=int(data.get("video_crf", 20)),
        overlay_background_alpha=float(data.get("overlay_background_alpha", 0.62)),
    )


def list_template_files() -> list[Path]:
    if not TEMPLATES.is_dir():
        return []
    return sorted(TEMPLATES.glob("*.json"))


def resolve_template_path(selection_label: str) -> Path | None:
    """Match combo box label 'name (file.json)' or plain stem to a path."""
    files = list_template_files()
    if not files:
        return None
    for p in files:
        try:
            lay = load_layout(p)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if selection_label == p.name or selection_label.startswith(lay.name):
            return p
    return files[0]
