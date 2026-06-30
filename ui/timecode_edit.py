from __future__ import annotations

from PySide6.QtWidgets import QLineEdit

from utils.timecode import format_timecode_digits


class TimecodeLineEdit(QLineEdit):
    """Line edit that inserts ':' after every two time digits."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._formatting = False
        self.textChanged.connect(self._format_as_timecode)

    def _format_as_timecode(self, text: str) -> None:
        if self._formatting:
            return

        cursor_from_end = len(text) - self.cursorPosition()
        formatted = format_timecode_digits(text)
        if formatted == text:
            return

        self._formatting = True
        self.setText(formatted)
        pos = max(0, len(formatted) - cursor_from_end)
        self.setCursorPosition(pos)
        self._formatting = False
