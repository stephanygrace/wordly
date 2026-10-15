"""Bundled fonts (assets/fonts, SIL OFL): available to Qt and installed for Resolve."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from utils.paths import ASSETS

FONTS_DIR = ASSETS / "fonts"
USER_FONTS_DIR = Path.home() / "Library" / "Fonts"

_registered = False


def bundled_fonts() -> list[Path]:
    return sorted(FONTS_DIR.glob("*.ttf"))


def register_bundled_fonts() -> None:
    """Make the bundled fonts usable by Qt (verse image rendering). Needs a Qt app."""
    global _registered
    if _registered:
        return
    from PySide6.QtGui import QFontDatabase

    for font in bundled_fonts():
        QFontDatabase.addApplicationFont(str(font))
    _registered = True


def install_fonts_for_resolve(target_dir: Path | None = None) -> list[Path]:
    """Copy bundled fonts into the user's Fonts folder (macOS) so Resolve's titles can use them.

    Returns the fonts newly installed; Resolve picks them up after its next restart.
    """
    if sys.platform != "darwin" and target_dir is None:
        return []
    target = target_dir or USER_FONTS_DIR
    target.mkdir(parents=True, exist_ok=True)
    installed: list[Path] = []
    for font in bundled_fonts():
        dest = target / font.name
        if not dest.exists():
            shutil.copy2(font, dest)
            installed.append(dest)
    return installed
