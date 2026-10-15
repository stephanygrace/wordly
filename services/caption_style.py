"""Caption style presets (assets/caption_styles.json) for the Resolve export."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

from utils.paths import ASSETS

STYLES_PATH = ASSETS / "caption_styles.json"
DEFAULT_STYLE_NAME = "Sermon reel"


@dataclass(frozen=True)
class CaptionStyle:
    name: str
    font: str = "Helvetica Neue"
    font_face: str = "Bold"
    font_size: int = 24
    text_color: str = "#FFFFFF"
    # Colour of the word being spoken; None shows the whole caption in text_color.
    highlight_color: str | None = None
    outline_color: str = "#000000"
    outline_width: int = 4  # 0 = no outline
    # Drop shadow (None = no shadow); offset/blur in pixels.
    shadow_color: str | None = None
    shadow_offset: int = 3
    shadow_blur: int = 3
    uppercase: bool = False
    # Percent of frame height from centre; negative moves toward the bottom.
    position_y: float = -25.0
    # Longer captions wrap onto two balanced lines (Resolve's text doesn't wrap).
    max_line_chars: int = 32
    # Words per caption when transcribing.
    max_words: int = 5

    def display_text(self, text: str) -> str:
        return text.upper() if self.uppercase else text


_BUILTIN = (
    # Matches the sermon reel template: small bold caps under the video, spoken word yellow.
    CaptionStyle(
        DEFAULT_STYLE_NAME,
        font="Outfit",
        font_size=24,
        max_line_chars=23,  # wrap to two lines before reaching the frame edge at this size
        highlight_color="#FFF000",
        outline_width=0,
        shadow_color="#000000C0",
        uppercase=True,
        position_y=-21.8,  # measured in Resolve: ~121px under the video, as in the template
        max_words=3,
    ),
    CaptionStyle("Word highlight", highlight_color="#FFD400", uppercase=True),
    CaptionStyle("Clean white"),
)


def hex_to_fcpxml(color: str) -> str:
    """'#RRGGBB' or '#RRGGBBAA' → FCPXML 'r g b a' (0..1 floats)."""
    value = color.strip().lstrip("#")
    if len(value) not in (6, 8):
        raise ValueError(f"Colour must look like #RRGGBB: {color!r}")
    channels = [int(value[i : i + 2], 16) / 255 for i in range(0, len(value), 2)]
    if len(channels) == 3:
        channels.append(1.0)
    return " ".join(f"{c:.4g}" for c in channels)


def load_caption_styles(path: Path | None = None) -> dict[str, CaptionStyle]:
    """Built-in styles plus any in caption_styles.json (same name overrides)."""
    styles = {s.name: s for s in _BUILTIN}
    source = path or STYLES_PATH
    if source.is_file():
        allowed = {f.name for f in fields(CaptionStyle)}
        for raw in json.loads(source.read_text(encoding="utf-8")).get("styles", []):
            values = {k: v for k, v in raw.items() if k in allowed}
            if values.get("name"):
                style = CaptionStyle(**values)
                for color in (style.text_color, style.outline_color, style.highlight_color, style.shadow_color):
                    if color:
                        hex_to_fcpxml(color)  # fail early on a typo
                styles[style.name] = style
    return styles


def get_caption_style(name: str | None, path: Path | None = None) -> CaptionStyle:
    styles = load_caption_styles(path)
    return styles.get(name or DEFAULT_STYLE_NAME) or styles[DEFAULT_STYLE_NAME]
