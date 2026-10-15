"""Locate the Resolve interchange template Wordly clones/patches at export time."""

from __future__ import annotations

import os
from pathlib import Path

from utils.paths import ASSETS

_ENV_TEMPLATE = "WORDLY_RESOLVE_TEMPLATE"
_PREFERRED_NAMES = (
    "sermon-highlights.fcpxml",
    "sermon-highlights.otio",
)


def templates_dir() -> Path:
    return ASSETS / "resolve_templates"


def template_path() -> Path | None:
    """Return the configured template path, or None if missing."""
    override = os.environ.get(_ENV_TEMPLATE, "").strip()
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None

    root = templates_dir()
    for name in _PREFERRED_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def template_available() -> bool:
    return template_path() is not None
