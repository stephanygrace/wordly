#!/usr/bin/env python3
from __future__ import annotations

import os
import platform
import sys


def _bootstrap_qt_env() -> None:
    """Tune Qt for WSL/WSLg so preview does not probe missing GPU/PipeWire stacks."""
    is_wsl = bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().lower()
    if not is_wsl:
        return
    os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
    os.environ.setdefault("QT_OPENGL", "software")
    os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
    os.environ.setdefault("QT_AUDIO_BACKEND", "alsa")
    # Let WSLg own the outer frame; Wordly draws its own title bar with min/max/close on WSL.
    os.environ.setdefault("QT_WAYLAND_DISABLE_WINDOWDECORATION", "1")


_bootstrap_qt_env()

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QStyleFactory

from ui.wizard_window import WizardWindow
from utils.paths import ensure_directories


def main() -> int:
    ensure_directories()
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        app.setStyle(fusion)
    app.setApplicationName("Wordly")
    app.setOrganizationName("Wordly")
    win = WizardWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
