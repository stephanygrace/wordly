"""Recent sermon paths persisted in QSettings (JSON list)."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings


def clear_recent_paths(settings: QSettings, key: str) -> None:
    settings.setValue(key, "[]")


def read_recent_paths(settings: QSettings, key: str) -> list[str]:
    raw = settings.value(key, "[]")
    try:
        data = json.loads(str(raw) if raw is not None else "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


def write_recent_paths(settings: QSettings, key: str, paths: list[str]) -> None:
    settings.setValue(key, json.dumps(paths))


def add_recent_path(settings: QSettings, key: str, path: Path, *, max_items: int = 8) -> None:
    resolved = str(path.resolve())
    cur = read_recent_paths(settings, key)
    cur = [p for p in cur if p != resolved]
    cur.insert(0, resolved)
    write_recent_paths(settings, key, cur[:max_items])


def existing_recent_files(settings: QSettings, key: str) -> list[Path]:
    """Paths that still exist on disk (newest first)."""
    out: list[Path] = []
    for s in read_recent_paths(settings, key):
        p = Path(s)
        if p.is_file():
            out.append(p)
    return out
