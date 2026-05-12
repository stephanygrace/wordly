"""Find incomplete yt-dlp / download artifacts under a folder."""

from __future__ import annotations

from pathlib import Path


def incomplete_download_paths(folder: Path) -> list[Path]:
    """
    Return files that are usually safe to delete after a cancelled or failed download.

    Includes ``*.part``, ``*.ytdl``, ``*.tmp``, ``*.temp``, and ``*.frag*`` matches.
    Only regular files directly under ``folder`` (not recursive).
    """
    if not folder.is_dir():
        return []
    found: set[Path] = set()
    for pattern in ("*.part", "*.ytdl", "*.tmp", "*.temp", "*.frag*", "*.f*mux.*"):
        for p in folder.glob(pattern):
            if p.is_file():
                found.add(p)
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def delete_paths(paths: list[Path]) -> tuple[int, list[str]]:
    """
    Delete each path that is a file. Returns (deleted_count, error_messages).
    """
    errors: list[str] = []
    n = 0
    for p in paths:
        try:
            if p.is_file():
                p.unlink()
                n += 1
        except OSError as exc:
            errors.append(f"{p.name}: {exc}")
    return n, errors
