#!/usr/bin/env python3
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow
from utils.paths import ensure_directories


def main() -> int:
    ensure_directories()
    app = QApplication(sys.argv)
    app.setApplicationName("Wordly")
    app.setOrganizationName("Wordly")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
