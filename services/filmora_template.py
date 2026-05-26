from __future__ import annotations

import os
from pathlib import Path

from utils.paths import ASSETS

TEMPLATE_DIR = ASSETS / "filmora_templates"
PREFERRED_TEMPLATE_NAMES = (
    "sermon-highlights.wfp",
    "filmora_14_2_9.wfp",
)


def template_path() -> Path:
    """
    Resolve the Filmora layout reference shipped under assets/filmora_templates/.

    Prefers ``sermon-highlights.wfp`` (GUI project + co-located media), then
  ``filmora_14_2_9.wfp``, then any other ``*.wfp`` in that folder.
    Override with env ``WORDLY_FILMORA_TEMPLATE`` (absolute path to a .wfp).
    """
    env_path = os.environ.get("WORDLY_FILMORA_TEMPLATE", "").strip()
    if env_path:
        resolved = Path(env_path).expanduser().resolve()
        if resolved.is_file():
            return resolved

    for name in PREFERRED_TEMPLATE_NAMES:
        candidate = TEMPLATE_DIR / name
        if candidate.is_file():
            return candidate.resolve()

    if TEMPLATE_DIR.is_dir():
        for candidate in sorted(TEMPLATE_DIR.glob("*.wfp")):
            if candidate.is_file() and ".bak" not in candidate.name.lower():
                return candidate.resolve()

    return (TEMPLATE_DIR / "filmora_14_2_9.wfp").resolve()


def template_media_bundle() -> dict[str, Path] | None:
    """
    Optional media beside the template (video.mp4, music.mp3, image.jpg).

    When present, sanitize and placeholder logic use these instead of tiny
    generated clips under reference_media/.
    """
    root = template_path().parent
    video = root / "video.mp4"
    music = root / "music.mp3"
    thumb = root / "image.jpg"
    if not video.is_file() or not music.is_file():
        return None
    bundle = {
        "highlight": video.resolve(),
        "source": video.resolve(),
        "music": music.resolve(),
        "thumb": thumb.resolve() if thumb.is_file() else music.resolve(),
    }
    return bundle


def template_available() -> bool:
    return template_path().is_file()
