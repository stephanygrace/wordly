from __future__ import annotations

from pathlib import Path

from utils.paths import ASSETS


def template_path() -> Path:
    return ASSETS / "filmora_templates" / "filmora_14_2_9.wfp"


def template_available() -> bool:
    return template_path().is_file()
