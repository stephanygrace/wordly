"""Small building blocks for the Wordly window: cards, sidebar steps, drop zone."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}


def repolish(widget: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def set_prop(widget: QWidget, name: str, value) -> None:
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    repolish(widget)
    for child in widget.findChildren(QWidget):
        repolish(child)


def label(text: str = "", name: str = "", *, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


def button(text: str, name: str = "", *, tooltip: str = "", on_click=None) -> QPushButton:
    btn = QPushButton(text)
    if name:
        btn.setObjectName(name)
    if tooltip:
        btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if on_click is not None:
        btn.clicked.connect(on_click)
    return btn


class Card(QFrame):
    """Rounded surface with an optional title row; add content to ``body``."""

    def __init__(self, title: str = "", subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(10)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        self.title_label: QLabel | None = None
        if title:
            self.title_label = label(title, "CardTitle")
            self.header.addWidget(self.title_label)
            self.header.addStretch(1)
            outer.addLayout(self.header)
        self.subtitle_label: QLabel | None = None
        if subtitle:
            self.subtitle_label = label(subtitle, "CardSubtitle", wrap=True)
            outer.addWidget(self.subtitle_label)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body)

    def set_title(self, text: str) -> None:
        if self.title_label is not None:
            self.title_label.setText(text)


class ResponsiveColumns(QWidget):
    """Side-by-side columns that stack vertically when the page gets narrow."""

    def __init__(self, *stretches: int, breakpoint: int = 820, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stretches = stretches
        self._breakpoint = breakpoint
        self._box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(14)
        self.cols: list[QVBoxLayout] = []
        for stretch in stretches:
            col = QVBoxLayout()
            col.setSpacing(14)
            self._box.addLayout(col, stretch)
            self.cols.append(col)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        width = event.size().width()
        horizontal = self._box.direction() == QBoxLayout.Direction.LeftToRight
        # A little hysteresis so a scroll bar appearing can't make it flip back and forth.
        if horizontal and width < self._breakpoint:
            self._box.setDirection(QBoxLayout.Direction.TopToBottom)
            for i in range(len(self._stretches)):
                self._box.setStretch(i, 0)
        elif not horizontal and width > self._breakpoint + 16:
            self._box.setDirection(QBoxLayout.Direction.LeftToRight)
            for i, stretch in enumerate(self._stretches):
                self._box.setStretch(i, stretch)


class StepItem(QFrame):
    clicked = Signal(int)

    def __init__(self, index: int, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StepItem")
        self._index = index
        self._clickable = False
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 9, 10, 9)
        row.setSpacing(10)
        self._badge = label(str(index + 1), "StepBadge")
        text = QVBoxLayout()
        text.setSpacing(1)
        self._name = label(title, "StepName")
        self._summary = label("", "StepSummary")
        self._summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addWidget(self._name)
        text.addWidget(self._summary)
        row.addWidget(self._badge, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(text, 1)

    def update_state(self, state: str, summary: str, clickable: bool) -> None:
        self._badge.setText("✓" if state == "done" else str(self._index + 1))
        self._summary.setText(summary)
        self._summary.setToolTip(summary)
        self._clickable = clickable
        self.setCursor(Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor)
        set_prop(self, "clickable", "true" if clickable else "false")
        set_prop(self, "state", state)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._index)
            event.accept()
            return
        super().mousePressEvent(event)


class StepSidebar(QWidget):
    """Vertical list of steps; finished and current steps can be clicked."""

    step_clicked = Signal(int)

    def __init__(self, titles: tuple[str, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(232)
        col = QVBoxLayout(self)
        col.setContentsMargins(14, 20, 14, 16)
        col.setSpacing(4)
        col.addWidget(label("Wordly", "AppName"))
        col.addWidget(label("Sermon highlight reels", "AppTagline"))
        col.addSpacing(18)
        self.items: list[StepItem] = []
        for i, title in enumerate(titles):
            item = StepItem(i, title)
            item.clicked.connect(self.step_clicked)
            self.items.append(item)
            col.addWidget(item)
        col.addStretch(1)
        self.footer = label("", "SidebarFooter", wrap=True)
        col.addWidget(self.footer)


class DropZone(QFrame):
    """Dashed area that accepts a dropped video file."""

    file_dropped = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(170)
        col = QVBoxLayout(self)
        col.setContentsMargins(20, 22, 20, 22)
        col.setSpacing(8)
        col.addStretch(1)
        icon = label("↓", "DropIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(icon)
        title = label("Drop a sermon video here", "CardTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(title)
        hint = label("MP4, MOV, MKV, WebM or M4V", "Faint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(hint)
        self.browse_button = button("Choose file…")
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.browse_button)
        row.addStretch(1)
        col.addSpacing(4)
        col.addLayout(row)
        col.addStretch(1)

    @staticmethod
    def _video_path(event) -> Path | None:
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                if path.suffix.lower() in VIDEO_EXTENSIONS:
                    return path
        return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._video_path(event) is not None:
            event.acceptProposedAction()
            set_prop(self, "hover", "true")

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        set_prop(self, "hover", "false")
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        set_prop(self, "hover", "false")
        path = self._video_path(event)
        if path is not None:
            event.acceptProposedAction()
            self.file_dropped.emit(path)


class ClipRow(QWidget):
    """One clip in the Highlights list: number, title and timing."""

    def __init__(self, index: int, title: str, meta: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 7, 8, 7)
        row.setSpacing(10)
        row.addWidget(label(str(index), "ClipIndex"), 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(1)
        title_lbl = label(title, "ClipTitle")
        title_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_lbl.setToolTip(title)
        text.addWidget(title_lbl)
        text.addWidget(label(meta, "ClipMeta"))
        row.addLayout(text, 1)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)


class SummaryRow(QFrame):
    """Export review line: mark, key, value and an Edit link."""

    edit_clicked = Signal()

    def __init__(self, key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SummaryRow")
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 10, 2, 10)
        row.setSpacing(12)
        self._mark = label("✓", "SummaryMark")
        self._mark.setFixedWidth(14)
        key_lbl = label(key, "SummaryKey")
        key_lbl.setFixedWidth(84)
        self._value = label("", "SummaryValue", wrap=True)
        self._value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        edit = button("Edit", "Link", on_click=self.edit_clicked.emit)
        row.addWidget(self._mark, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(key_lbl, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self._value, 1, Qt.AlignmentFlag.AlignTop)
        row.addWidget(edit, 0, Qt.AlignmentFlag.AlignTop)

    def set_value(self, text: str, tone: str = "ok") -> None:
        self._value.setText(text)
        self._mark.setText({"ok": "✓", "todo": "!", "off": "–"}.get(tone, "✓"))
        set_prop(self._mark, "tone", tone)
