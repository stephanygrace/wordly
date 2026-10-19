from __future__ import annotations

import os
import platform
import re
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtGui import QCursor, QKeySequence, QMoveEvent, QPixmap, QResizeEvent, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from models.project import ClipSegment, MusicChoice, ProjectState, VerseChoice
from services.downloader import download_backend_description, download_facebook_video
from services.multi_clip import export_clips, merge_clips
from services.music_downloader import AUDIO_EXTENSIONS, download_instrumental, library_tracks, track_title
from services.photo_downloader import IMAGE_EXTENSIONS, download_photo
from services.trimmer import ffprobe_duration_seconds
from services.resolve_export import END_PHOTO_S, generate_resolve_project
from services.resolve_launcher import install_import_script, open_resolve_project, reveal_in_finder
from services.resolve_template import template_available
from services.claude_client import MissingApiKey, api_key, save_api_key
from services.highlights import (
    LUX_LEDGER,
    MAX_TOTAL_S,
    HighlightPlan,
    find_highlights,
    find_sermon,
    format_clock,
    usage_totals,
)
from services.highlights import format_timecode as highlight_timecode
from services.caption_style import DEFAULT_STYLE_NAME, CaptionStyle, load_caption_styles
from services.transcribe import Caption, transcribe_clips, whisper_backend_label, whisper_model_name, word_timings
from ui import theme
from ui.preview_player import PreviewPlayer
from ui.timecode_edit import TimecodeLineEdit
from ui.widgets import Card, ClipRow, DropZone, ResponsiveColumns, StepSidebar, SummaryRow, button, label, set_prop
from utils.app_settings import KEY_AUTO_CAPTIONS, KEY_CAPTION_STYLE, KEY_LAST_FB_URL, KEY_REEL_LAYOUT, settings
from utils.fonts import install_fonts_for_resolve, register_bundled_fonts
from utils.console_log import log_error, log_info, log_progress, log_step, log_warn
from utils.export_name import default_export_project_name
from utils.paths import CLIPS, DOWNLOADS, ensure_directories
from utils.timecode import (
    end_timecode_from_start_offset,
    format_timecode,
    normalize_four_digit_timecode,
    parse_timecode,
    parse_timestamp_list,
    validate_segment_times,
)

S_SOURCE, S_HIGHLIGHTS, S_CAPTIONS, S_VERSE_MUSIC, S_EXPORT = range(5)

# (sidebar name, page title, page subtitle)
_STEPS = (
    ("Source", "Bring in the service", "Paste a Facebook or YouTube link, or use a video already on this computer."),
    ("Highlights", "Pick the moments", "Let Lux choose the highlights, or mark your own clips on the player."),
    ("Captions", "Add captions", "Transcribe the clips, fix any wrong words, and choose how captions look."),
    ("Verse & music", "Verse and music", "The verse sits above the video; the instrumental plays underneath."),
    ("Export", "Review and export", "Check everything, then build the DaVinci Resolve project."),
)

# "John 3:16", "1 Corinthians 13:4-7", "Awit 23:1" on the first line of a pasted verse.
_VERSE_REFERENCE_RE = re.compile(r"^[1-3]?\s*[^\W\d_][\w .]*\s\d+:\d+")
_CAPTION_TIME_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*s?\s*$")


class _JobWorker(QObject):
    finished_ok = Signal(object)
    failed = Signal(str)
    progress = Signal(float, str)

    def __init__(self, fn, *args, **kwargs) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._cancel = threading.Event()

    def run(self) -> None:
        try:
            result = self._fn(
                *self._args,
                progress_cb=lambda r, m: self.progress.emit(r, m),
                should_cancel=self._cancel.is_set,
                **self._kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(result)

    def cancel(self) -> None:
        self._cancel.set()


def _running_on_wsl() -> bool:
    return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().lower()


_WSL_RESIZE_MARGIN = 8


def _cursor_for_resize_edges(edges: Qt.Edge) -> Qt.CursorShape | None:
    if edges == Qt.Edge(0):
        return None
    if edges in (Qt.Edge.LeftEdge, Qt.Edge.RightEdge):
        return Qt.CursorShape.SizeHorCursor
    if edges in (Qt.Edge.TopEdge, Qt.Edge.BottomEdge):
        return Qt.CursorShape.SizeVerCursor
    if edges in (
        Qt.Edge.LeftEdge | Qt.Edge.TopEdge,
        Qt.Edge.RightEdge | Qt.Edge.BottomEdge,
    ):
        return Qt.CursorShape.SizeFDiagCursor
    if edges in (
        Qt.Edge.RightEdge | Qt.Edge.TopEdge,
        Qt.Edge.LeftEdge | Qt.Edge.BottomEdge,
    ):
        return Qt.CursorShape.SizeBDiagCursor
    return None


class _WslTitleBar(QWidget):
    """In-window title bar for WSLg where system + Qt decorations would stack."""

    def __init__(self, window: QMainWindow, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._window = window
        self._drag_origin: QPoint | None = None

        self.setObjectName("WslTitleBar")
        self.setFixedHeight(32)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 0, 6, 0)
        row.setSpacing(6)

        self._title_label = QLabel(title)
        self._title_label.setObjectName("WslTitleLabel")

        self._min_btn = QPushButton("─")
        self._max_btn = QPushButton("□")
        self._close_btn = QPushButton("✕")
        for btn in (self._min_btn, self._max_btn, self._close_btn):
            btn.setObjectName("WslTitleButton")
            btn.setFixedSize(32, 24)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setObjectName("WslTitleCloseButton")

        self._min_btn.clicked.connect(window.showMinimized)
        self._max_btn.clicked.connect(self._toggle_maximize)
        self._close_btn.clicked.connect(window.close)

        row.addWidget(self._title_label, stretch=1)
        row.addWidget(self._min_btn)
        row.addWidget(self._max_btn)
        row.addWidget(self._close_btn)

    def _toggle_maximize(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
            self._max_btn.setText("□")
        else:
            self._window.showMaximized()
            self._max_btn.setText("❐")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.position().toPoint())
            if isinstance(child, QPushButton):
                super().mousePressEvent(event)
                return
            if self._window._try_wsl_resize_at_global(event.globalPosition().toPoint()):
                event.accept()
                return
            handle = self._window.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
            self._drag_origin = event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (
            self._drag_origin is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            if self._window.isMaximized():
                self._window.showNormal()
                self._max_btn.setText("□")
            self._window.move(event.globalPosition().toPoint() - self._drag_origin)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def _seconds(text: str) -> float:
    return parse_timecode(text).total_seconds


def _short_clock(seconds: float) -> str:
    """1:05:30 for long times, 4:12 under an hour."""
    clock = format_clock(seconds)
    return clock[2:] if clock.startswith("0:") else clock


class _SpaceTogglesPlayback(QObject):
    """Space plays/pauses instead of starting an edit that replaces the cell.

    Inside an open cell editor Space still types a space.
    """

    def __init__(self, table: QTableWidget, toggle) -> None:
        super().__init__(table)
        self._table = table
        self._toggle = toggle

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if (
            event.type() in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress)
            and event.key() == Qt.Key.Key_Space
            and not event.modifiers()
            and self._table.state() != QAbstractItemView.State.EditingState
        ):
            if event.type() == QEvent.Type.KeyPress:
                self._toggle()
            event.accept()
            return True
        return super().eventFilter(watched, event)


class WizardWindow(QMainWindow):
    """Production wizard for sermon highlight reels."""

    _STEP_TITLES = tuple(name for name, _, _ in _STEPS)

    # Background ffprobe signals (emitted from a daemon thread, consumed on UI thread)
    _sermon_probed = Signal(float)
    _sermon_probe_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        ensure_directories()
        self.setWindowTitle("Wordly — Sermon Highlight Studio")
        self.resize(1180, 760)
        self.setMinimumSize(980, 640)
        self._project = ProjectState()
        self._thread: QThread | None = None
        self._worker: _JobWorker | None = None
        # Threads of jobs that were replaced before they had fully stopped. Python
        # owns job threads and workers (no deleteLater): starting a job right as
        # the previous one finished used to free a thread Qt was still using and
        # crashed the app, so threads are only released once really stopped.
        self._retired_jobs: list[tuple[QThread, _JobWorker | None]] = []
        self._job_on_ok = None
        self._job_on_fail = None
        self._busy = False
        self._last_step_index = 0
        self._editing_row: int | None = None
        self._last_export_path: Path | None = None
        self._wsl_repaint_hardening = _running_on_wsl()

        self._sidebar = StepSidebar(self._STEP_TITLES)
        self._sidebar.step_clicked.connect(self._go_to_step)

        self._stack = QStackedWidget()
        self._stack.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._stack.setAutoFillBackground(True)
        self._steps = [
            self._wrap_step(self._build_source_step()),
            self._wrap_step(self._build_highlights_step()),
            self._wrap_step(self._build_captions_step()),
            self._wrap_step(self._build_verse_music_step()),
            self._wrap_step(self._build_export_step()),
        ]
        for widget in self._steps:
            self._stack.addWidget(widget)
        self._stack.currentChanged.connect(self._on_step_changed)

        if self._wsl_repaint_hardening:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        if self._wsl_repaint_hardening:
            self._title_bar = _WslTitleBar(self, "Wordly — Sermon Highlight Studio")
            outer.addWidget(self._title_bar)

        body = QWidget()
        body_row = QHBoxLayout(body)
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(0)
        body_row.addWidget(self._sidebar)

        main = QWidget()
        main.setObjectName("MainArea")
        main_col = QVBoxLayout(main)
        main_col.setContentsMargins(0, 0, 0, 0)
        main_col.setSpacing(0)
        main_col.addWidget(self._build_page_header())
        main_col.addWidget(self._stack, stretch=1)
        main_col.addWidget(self._build_footer())
        body_row.addWidget(main, stretch=1)
        outer.addWidget(body, stretch=1)

        self.setCentralWidget(root)
        for surface in (root, body, main):
            self._configure_opaque_surfaces(surface)

        self.setPalette(theme.palette())
        self.setStyleSheet(theme.stylesheet())
        self._apply_pointer_cursors()
        primary_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        primary_shortcut.activated.connect(self._primary_shortcut)
        self._restore_prefs()
        # Resolve only scans its Scripts menu at startup, so install the importer early.
        try:
            install_import_script()
        except Exception as exc:  # noqa: BLE001
            log_warn("export", f"Could not install Resolve import script: {exc}")
        # Caption/verse fonts: Qt needs them registered, Resolve needs them installed.
        try:
            register_bundled_fonts()
            newly = install_fonts_for_resolve()
            if newly:
                log_step("export", f"Installed fonts for Resolve: {', '.join(p.name for p in newly)} (restart Resolve to use them)")
        except Exception as exc:  # noqa: BLE001
            log_warn("export", f"Could not install caption fonts: {exc}")
        self._sermon_probed.connect(self._on_sermon_probed, Qt.ConnectionType.QueuedConnection)
        self._sermon_probe_failed.connect(self._on_sermon_probe_failed, Qt.ConnectionType.QueuedConnection)
        self._on_step_changed(0)
        if self._wsl_repaint_hardening:
            app = QApplication.instance()
            if app is not None:
                app.installEventFilter(self)

    # --- Chrome ------------------------------------------------------------

    def _wsl_resize_edges(self, local_pos: QPoint) -> Qt.Edge:
        margin = _WSL_RESIZE_MARGIN
        edges = Qt.Edge(0)
        if local_pos.x() <= margin:
            edges |= Qt.Edge.LeftEdge
        if local_pos.x() >= self.width() - margin:
            edges |= Qt.Edge.RightEdge
        if local_pos.y() <= margin:
            edges |= Qt.Edge.TopEdge
        if local_pos.y() >= self.height() - margin:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _try_wsl_resize_at_global(self, global_pos: QPoint) -> bool:
        edges = self._wsl_resize_edges(self.mapFromGlobal(global_pos))
        if edges == Qt.Edge(0):
            return False
        handle = self.windowHandle()
        if handle is None:
            return False
        return handle.startSystemResize(edges)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if not self._wsl_repaint_hardening:
            return super().eventFilter(watched, event)
        if not isinstance(watched, QWidget) or watched.window() is not self:
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            if self._try_wsl_resize_at_global(event.globalPosition().toPoint()):
                return True
        elif event.type() == QEvent.Type.MouseMove and not (event.buttons() & Qt.MouseButton.LeftButton):
            cursor = _cursor_for_resize_edges(
                self._wsl_resize_edges(self.mapFromGlobal(event.globalPosition().toPoint()))
            )
            if cursor is not None:
                self.setCursor(QCursor(cursor))
            else:
                self.unsetCursor()
        return super().eventFilter(watched, event)

    def _build_page_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("PageHeader")
        col = QVBoxLayout(header)
        col.setContentsMargins(32, 24, 32, 14)
        col.setSpacing(4)
        self._page_eyebrow = label("", "PageEyebrow")
        self._page_title = label("", "PageTitle")
        self._page_subtitle = label("", "PageSubtitle", wrap=True)
        col.addWidget(self._page_eyebrow)
        col.addWidget(self._page_title)
        col.addWidget(self._page_subtitle)
        return header

    def _build_footer(self) -> QWidget:
        footer = QWidget()
        footer.setObjectName("Footer")
        footer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col = QVBoxLayout(footer)
        col.setContentsMargins(32, 12, 32, 14)
        col.setSpacing(10)

        self._job_bar = QFrame()
        self._job_bar.setObjectName("JobBar")
        job_row = QHBoxLayout(self._job_bar)
        job_row.setContentsMargins(14, 10, 10, 10)
        job_row.setSpacing(12)
        job_text = QVBoxLayout()
        job_text.setSpacing(6)
        self._status = label("Ready", "JobStatusLabel")
        self._progress = QProgressBar()
        self._progress.setRange(0, 1000)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setFormat("Ready")
        job_text.addWidget(self._status)
        job_text.addWidget(self._progress)
        self._cancel_btn = button("Cancel", "Cancel", on_click=self._cancel_job)
        job_row.addLayout(job_text, 1)
        job_row.addWidget(self._cancel_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self._job_bar.setVisible(False)
        col.addWidget(self._job_bar)

        nav = QHBoxLayout()
        nav.setSpacing(10)
        self._back_btn = button("← Back", "Ghost", on_click=self._go_back)
        self._nav_hint = label("", "Faint")
        self._next_btn = button("Continue", "Primary", on_click=self._go_next)
        self._next_btn.setMinimumWidth(150)
        self._next_btn.setToolTip("Ctrl+Enter")
        nav.addWidget(self._back_btn)
        nav.addStretch(1)
        nav.addWidget(self._nav_hint)
        nav.addWidget(self._next_btn)
        col.addLayout(nav)
        return footer

    @staticmethod
    def _wrap_step(inner: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        scroll.setAutoFillBackground(True)
        viewport = scroll.viewport()
        viewport.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        viewport.setAutoFillBackground(True)
        inner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        inner.setAutoFillBackground(True)
        inner.setObjectName("Page")
        container = QWidget()
        container.setObjectName("PageContainer")
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(32, 4, 32, 20)
        container_layout.setSpacing(0)
        container_layout.addWidget(inner)
        container_layout.addStretch(1)
        container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        container.setAutoFillBackground(True)
        scroll.setWidget(container)
        return scroll

    @staticmethod
    def _configure_opaque_surfaces(root: QWidget) -> None:
        root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root.setAutoFillBackground(True)

    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        if self._wsl_repaint_hardening:
            QTimer.singleShot(0, self._request_full_repaint)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._wsl_repaint_hardening:
            QTimer.singleShot(0, self._request_full_repaint)

    def _request_full_repaint(self) -> None:
        handle = self.windowHandle()
        if handle is not None:
            handle.requestUpdate()
        self.update()
        root = self.centralWidget()
        if root is not None:
            root.update()
        self._stack.update()

    def _apply_pointer_cursors(self) -> None:
        root = self.centralWidget()
        if root is None:
            return
        for btn in root.findChildren(QPushButton) + root.findChildren(QToolButton):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)

    @staticmethod
    def _page() -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        return page, layout

    @staticmethod
    def _columns(*stretches: int, breakpoint: int = 820) -> tuple[ResponsiveColumns, list[QVBoxLayout]]:
        row = ResponsiveColumns(*stretches, breakpoint=breakpoint)
        return row, row.cols

    # --- Step 1: Source ----------------------------------------------------

    def _build_source_step(self) -> QWidget:
        page, layout = self._page()

        self._source_loaded_card = Card()
        set_prop(self._source_loaded_card, "tone", "success")
        loaded_row = QHBoxLayout()
        loaded_text = QVBoxLayout()
        loaded_text.setSpacing(2)
        self._source_loaded_title = label("", "CardTitle")
        self._download_result = label("No sermon loaded yet.", "Muted", wrap=True)
        loaded_text.addWidget(self._source_loaded_title)
        loaded_text.addWidget(self._download_result)
        loaded_row.addLayout(loaded_text, 1)
        loaded_row.addWidget(
            button("Pick highlights →", "Primary", on_click=lambda: self._go_to_step(S_HIGHLIGHTS)),
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        self._source_loaded_card.body.addLayout(loaded_row)
        self._source_loaded_card.setVisible(False)
        layout.addWidget(self._source_loaded_card)

        link_card = Card("Paste a link", "Facebook Live, Facebook video or YouTube. Previously downloaded services open instantly.")
        self._url_edit = QLineEdit()
        self._url_edit.setObjectName("BigInput")
        self._url_edit.setPlaceholderText("https://www.facebook.com/…  or  https://youtu.be/…")
        self._url_edit.setClearButtonEnabled(True)
        self._url_edit.returnPressed.connect(self._start_download)
        self._download_btn = button("Download", "Primary", on_click=self._start_download)
        self._download_btn.setMinimumHeight(40)
        url_row = QHBoxLayout()
        url_row.setSpacing(8)
        url_row.addWidget(self._url_edit, 1)
        url_row.addWidget(self._download_btn)
        link_card.body.addLayout(url_row)
        self._download_backend_note = label(f"Downloads with {download_backend_description()}.", "Faint", wrap=True)
        link_card.body.addWidget(self._download_backend_note)
        layout.addWidget(link_card)

        or_label = label("or", "Faint")
        or_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(or_label)

        self._drop_zone = DropZone()
        self._open_local_btn = self._drop_zone.browse_button
        self._open_local_btn.clicked.connect(self._open_local_sermon)
        self._drop_zone.file_dropped.connect(self._set_sermon)
        layout.addWidget(self._drop_zone)
        return page

    # --- Step 2: Highlights ------------------------------------------------

    def _build_highlights_step(self) -> QWidget:
        page, layout = self._page()
        row, (left, right) = self._columns(3, 2, breakpoint=800)
        layout.addWidget(row)

        preview_card = Card("Preview")
        self._seg_duration_hint = label("No video", "Pill")
        preview_card.header.addWidget(self._seg_duration_hint)
        self._preview = PreviewPlayer()
        self._preview.duration_changed_s.connect(self._on_preview_duration)
        self._preview.attach_space_shortcut(page)
        preview_card.body.addWidget(self._preview)
        left.addWidget(preview_card)

        self._composer_card = Card("Mark a clip")
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        self._seg_start = TimecodeLineEdit()
        self._seg_start.setPlaceholderText("1:25:00")
        self._seg_end = TimecodeLineEdit()
        self._seg_end.setPlaceholderText("1:25:30")
        self._seg_label = QLineEdit()
        self._seg_label.setPlaceholderText("Title (optional), e.g. God's plan")
        for field in (self._seg_start, self._seg_end):
            field.setMinimumWidth(96)
            field.setToolTip("Type digits; colons are added for you (e.g. 12500 → 1:25:00)")
        playhead_tip = "Set to the player's current position"
        start_now = button("◉ Now", "Small", tooltip=playhead_tip, on_click=lambda: self._mark_from_playhead(self._seg_start))
        end_now = button("◉ Now", "Small", tooltip=playhead_tip, on_click=lambda: self._mark_from_playhead(self._seg_end))
        self._seg_end_30_btn = button("30s", "Small", tooltip="End 30 seconds after the start", on_click=lambda: self._set_segment_end_offset(30))
        self._seg_end_60_btn = button("60s", "Small", tooltip="End 60 seconds after the start", on_click=lambda: self._set_segment_end_offset(60))
        grid.addWidget(label("Start", "FieldLabel"), 0, 0)
        grid.addWidget(label("End", "FieldLabel"), 0, 2)
        grid.addWidget(self._seg_start, 1, 0)
        grid.addWidget(start_now, 1, 1)
        grid.addWidget(self._seg_end, 1, 2)
        grid.addWidget(end_now, 1, 3)
        grid.addWidget(self._seg_label, 2, 0, 1, 4)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        self._composer_card.body.addLayout(grid)
        self._seg_error = label("", "FieldError", wrap=True)
        self._seg_error.setVisible(False)
        self._composer_card.body.addWidget(self._seg_error)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._add_segment_btn = button("Add clip", "Primary", on_click=self._add_segment)
        self._preview_range_btn = button("▶ Play range", tooltip="Play the start–end range in the preview", on_click=self._preview_typed_range)
        self._cancel_edit_btn = button("Cancel", "Ghost", on_click=lambda: self._select_segment(None))
        actions.addWidget(self._add_segment_btn)
        actions.addWidget(self._preview_range_btn)
        actions.addWidget(self._cancel_edit_btn)
        actions.addStretch(1)
        actions.addWidget(label("Length", "Faint"))
        actions.addWidget(self._seg_end_30_btn)
        actions.addWidget(self._seg_end_60_btn)
        self._composer_card.body.addLayout(actions)
        left.addWidget(self._composer_card)
        left.addStretch(1)

        lux_card = Card(
            "Let Lux pick",
            "Lux picks 2–3 highlights (2 minutes max) plus the Bible verse. To narrow the search, "
            "trim first: Find sermon cuts out the songs for free, or add a clip around the part to "
            "scan, and Lux looks only there. Or paste a list of times and Lux scans 60 s from each. "
            "With no clips, Lux scans the whole video.",
        )
        set_prop(lux_card, "accent", "true")
        self._find_sermon_btn = button(
            "Find sermon",
            tooltip="Free, no Lux: skips the opening songs and adds the sermon as a clip you can adjust.",
            on_click=self._find_sermon,
        )
        self._find_highlights_btn = button("✦  Let Lux pick highlights", "Primary", on_click=lambda: self._find_highlights())
        self._paste_times_btn = button(
            "Paste times",
            tooltip="Paste start times (e.g. 101, 149 = 1:01:00, 1:49:00); each becomes a 60 s clip Lux scans.",
            on_click=self._paste_timestamps,
        )
        lux_actions = QHBoxLayout()
        lux_actions.addWidget(self._find_sermon_btn)
        lux_actions.addWidget(self._paste_times_btn)
        lux_actions.addWidget(self._find_highlights_btn, 1)
        lux_card.body.addLayout(lux_actions)
        self._highlights_summary = label("", "Notice", wrap=True)
        set_prop(self._highlights_summary, "tone", "lux")
        self._highlights_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._highlights_summary.setVisible(False)
        lux_card.body.addWidget(self._highlights_summary)
        self._lux_spend = label("", "Faint", wrap=True)
        self._lux_spend.setToolTip(f"Every Lux run is logged in {LUX_LEDGER.relative_to(LUX_LEDGER.parents[1])}.")
        lux_card.body.addWidget(self._lux_spend)
        self._refresh_lux_spend()
        right.addWidget(lux_card)

        clips_card = Card("Clips")
        self._clips_total_pill = label("", "Pill")
        clips_card.header.addWidget(self._clips_total_pill)
        self._segment_list = QListWidget()
        self._segment_list.setObjectName("SegmentList")
        self._segment_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._segment_list.setMinimumHeight(170)
        self._segment_list.currentRowChanged.connect(self._on_segment_row_changed)
        clips_card.body.addWidget(self._segment_list)
        self._clips_empty = label(
            "No clips yet. Ask Lux, or mark a start and end on the player and click Add clip.",
            "Muted",
            wrap=True,
        )
        clips_card.body.addWidget(self._clips_empty)
        clip_actions = QHBoxLayout()
        clip_actions.setSpacing(8)
        self._new_clip_btn = button("＋ New", "Small", on_click=lambda: self._select_segment(None))
        self._remove_seg_btn = button("Remove", "Danger", on_click=self._remove_segment)
        self._save_clips_btn = QToolButton()
        self._save_clips_btn.setText("Save video ▾")
        self._save_clips_btn.setToolTip("Save the clips as video files without making a Resolve project")
        self._save_clips_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        save_menu = QMenu(self._save_clips_btn)
        save_menu.addAction("Separate clips…", self._export_trimmed_clips_clicked)
        save_menu.addAction("One merged video…", self._export_merged_video_clicked)
        self._save_clips_btn.setMenu(save_menu)
        clip_actions.addWidget(self._new_clip_btn)
        clip_actions.addWidget(self._remove_seg_btn)
        clip_actions.addStretch(1)
        clip_actions.addWidget(self._save_clips_btn)
        clips_card.body.addLayout(clip_actions)
        right.addWidget(clips_card)
        right.addStretch(1)

        self._seg_start.textChanged.connect(self._on_timestamp_fields_changed)
        self._seg_end.textChanged.connect(self._on_timestamp_fields_changed)
        self._refresh_segment_list()
        return page

    # --- Step 3: Captions --------------------------------------------------

    def _build_captions_step(self) -> QWidget:
        page, layout = self._page()

        backend = whisper_backend_label()
        toggle_card = Card()
        self._auto_captions_check = QCheckBox("Add captions to the reel")
        self._auto_captions_check.setObjectName("BigToggle")
        self._auto_captions_check.setChecked(
            backend is not None and settings().value(KEY_AUTO_CAPTIONS, False, type=bool)
        )
        self._auto_captions_check.setEnabled(backend is not None)
        self._auto_captions_check.toggled.connect(self._on_captions_toggled)
        toggle_card.body.addWidget(self._auto_captions_check)
        if backend is None:
            backend_text = "Install mlx-whisper or faster-whisper to turn captions on."
        else:
            backend_text = (
                f"Transcribed on this computer with {backend} ({whisper_model_name()} model). "
                "The first run downloads the model."
            )
        toggle_card.body.addWidget(label(backend_text, "Muted", wrap=True))
        layout.addWidget(toggle_card)

        # Transcript first, the clip preview beside it on the right.
        row, (left, right) = self._columns(3, 2)
        layout.addWidget(row)

        try:
            self._caption_styles = load_caption_styles()
        except (ValueError, TypeError) as exc:
            log_warn("captions", f"caption_styles.json ignored: {exc}")
            self._caption_styles = {}
        if not self._caption_styles:
            self._caption_styles = {DEFAULT_STYLE_NAME: CaptionStyle(DEFAULT_STYLE_NAME)}
        self._style_card = Card("Preview")
        self._caption_style_combo = QComboBox()
        self._caption_style_combo.addItems(list(self._caption_styles))
        saved_style = settings().value(KEY_CAPTION_STYLE, DEFAULT_STYLE_NAME, type=str)
        if saved_style in self._caption_styles:
            self._caption_style_combo.setCurrentText(saved_style)
        self._project.caption_style = self._caption_style_combo.currentText()
        self._caption_style_combo.currentTextChanged.connect(self._on_caption_style_changed)
        self._caption_preview = QLabel()
        self._caption_preview.setObjectName("CaptionStage")
        self._caption_preview.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        self._caption_preview.setWordWrap(True)
        self._caption_preview.setMinimumHeight(110)
        self._caption_preview_index = 0
        self._caption_preview_timer = QTimer(self)
        self._caption_preview_timer.setInterval(450)
        self._caption_preview_timer.timeout.connect(self._tick_caption_preview)
        # Check captions against the clip: click a caption to play from it; the caption
        # under the video follows playback with the spoken word lit.
        self._caption_player = PreviewPlayer()
        self._caption_player.position_changed_ms.connect(self._on_caption_player_position)
        self._caption_clip_idx: int | None = None
        self._caption_view_clip = 0  # the clip being reviewed: its captions in the table, it in the preview
        self._live_caption: tuple[int, int, float] | None = None  # clip, caption, seconds
        self._caption_rows: dict[tuple[int, int], int] = {}
        self._style_card.body.addWidget(self._caption_style_combo)
        self._style_card.body.addWidget(self._caption_player)
        self._style_card.body.addWidget(self._caption_preview)
        self._style_card.body.addWidget(
            label(
                "Click a caption to play the clip from there and check it against what's said. "
                "Add your own looks in assets/caption_styles.json.",
                "Faint",
                wrap=True,
            )
        )
        right.addWidget(self._style_card)
        right.addStretch(1)

        self._transcript_card = Card("Transcript")
        self._transcribe_btn = button("Transcribe", "PrimarySmall", on_click=lambda: self._start_transcription())
        self._insert_caption_btn = button(
            "＋ Insert line",
            "Small",
            tooltip="Add a caption for missing words: pause the clip where they're said, then click.",
            on_click=self._insert_caption_line,
        )
        self._delete_caption_btn = button("Delete line", "Small", tooltip="Remove the selected caption", on_click=self._delete_caption_line)
        self._transcript_card.header.addWidget(self._insert_caption_btn)
        self._transcript_card.header.addWidget(self._delete_caption_btn)
        self._transcript_card.header.addWidget(self._transcribe_btn)
        # One clip at a time: pick it here; the table and the preview follow.
        self._caption_clip_tabs = QTabBar()
        self._caption_clip_tabs.setObjectName("ClipTabs")
        self._caption_clip_tabs.setExpanding(False)
        self._caption_clip_tabs.setDrawBase(False)
        self._caption_clip_tabs.setVisible(False)
        self._caption_clip_tabs.currentChanged.connect(self._on_caption_clip_tab)
        self._transcript_card.body.addWidget(self._caption_clip_tabs)
        self._captions_status = label("", "Muted", wrap=True)
        self._transcript_card.body.addWidget(self._captions_status)
        self._captions_table = QTableWidget(0, 3)
        self._captions_table.setHorizontalHeaderLabels(["CLIP", "TIME", "CAPTION — double-click to edit"])
        self._captions_table.verticalHeader().setVisible(False)
        self._captions_table.setShowGrid(False)
        self._captions_table.setAlternatingRowColors(False)
        header = self._captions_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._captions_table.setColumnHidden(0, True)  # the clip is chosen with the tabs above
        self._captions_table.setMinimumHeight(260)
        self._captions_table.itemChanged.connect(self._on_caption_edited)
        self._captions_table.currentCellChanged.connect(lambda row, *_: self._play_caption_row(row))
        # Typing on a selected row used to replace its text (a stray Space blanked captions).
        self._captions_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self._captions_table.installEventFilter(
            _SpaceTogglesPlayback(self._captions_table, self._toggle_caption_playback)
        )
        self._caption_player.attach_space_shortcut(page)
        self._transcript_card.body.addWidget(self._captions_table)
        left.addWidget(self._transcript_card)

        self._render_caption_preview()
        return page

    def _selected_caption_style(self) -> CaptionStyle:
        name = self._caption_style_combo.currentText()
        return self._caption_styles.get(name) or next(iter(self._caption_styles.values()))

    def _on_caption_style_changed(self, name: str) -> None:
        settings().setValue(KEY_CAPTION_STYLE, name)
        self._project.caption_style = name
        self._caption_preview_index = 0
        self._render_caption_preview()
        self._update_nav()

    def _tick_caption_preview(self) -> None:
        self._caption_preview_index += 1
        self._render_caption_preview()

    def _render_caption_preview(self) -> None:
        """Approximate the caption look; the highlight cycles like it will in Resolve."""
        style = self._selected_caption_style()
        live = self._live_caption_html(style)
        if live is not None:
            # Following the clip: show what's on screen at the playhead.
            self._caption_preview.setText(live)
            self._caption_preview_timer.stop()
            return
        sample = "God is good all the time"
        clips = self._project.clip_captions
        if self._project.captions_current() and self._caption_view_clip < len(clips):
            first = next(iter(clips[self._caption_view_clip]), None)
            if first is not None:
                sample = first.text
        words = style.display_text(sample).split()
        lit = self._caption_preview_index % max(1, len(words)) if style.highlight_color else -1
        html = " ".join(
            f'<span style="color:{style.highlight_color if i == lit else style.text_color}">{w}</span>'
            for i, w in enumerate(words)
        )
        self._caption_preview.setText(html)
        if style.highlight_color and self._stack.currentIndex() == S_CAPTIONS and self._captions_enabled():
            self._caption_preview_timer.start()
        else:
            self._caption_preview_timer.stop()

    def _live_caption_html(self, style: CaptionStyle) -> str | None:
        if self._live_caption is None or not self._project.captions_current():
            return None
        clip_idx, cap_idx, t = self._live_caption
        try:
            caption = self._project.clip_captions[clip_idx][cap_idx]
        except IndexError:
            return None
        if not (caption.start - 0.05 <= t < caption.end + 0.3):
            return ""  # between captions: nothing on screen
        tokens = word_timings(caption)
        lit = next((i for i, (_, a, b) in enumerate(tokens) if a <= t < b), len(tokens) - 1 if t >= caption.end else -1)
        return " ".join(
            f'<span style="color:{style.highlight_color if style.highlight_color and i == lit else style.text_color}">'
            f"{style.display_text(word)}</span>"
            for i, (word, _, _) in enumerate(tokens)
        )

    def _load_caption_clip(self, clip_idx: int) -> bool:
        clips = self._project.clip_paths
        if not (0 <= clip_idx < len(clips)) or not clips[clip_idx].exists():
            return False
        if self._caption_clip_idx != clip_idx:
            # Set first: loading reports position 0, which must be read as this clip's.
            self._caption_clip_idx = clip_idx
            self._caption_player.load_file(clips[clip_idx])
        return True

    def _play_caption_row(self, row: int) -> None:
        item = self._captions_table.item(row, 2) if row >= 0 else None
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not key or not self._project.captions_current():
            return
        clip_idx, cap_idx = key
        if not self._load_caption_clip(clip_idx):
            return
        caption = self._project.clip_captions[clip_idx][cap_idx]
        self._live_caption = (clip_idx, cap_idx, caption.start)
        self._caption_player.seek_seconds(caption.start)
        self._caption_player.play()
        self._render_caption_preview()

    def _toggle_caption_playback(self) -> None:
        if self._caption_clip_idx is None:
            self._play_caption_row(max(0, self._captions_table.currentRow()))
        else:
            self._caption_player.toggle_play_pause()

    def _on_caption_player_position(self, ms: int) -> None:
        if self._caption_clip_idx is None or not self._project.captions_current():
            return
        clip_idx, t = self._caption_clip_idx, ms / 1000.0
        captions = self._project.clip_captions[clip_idx]
        # The caption on screen: the last one that has started.
        cap_idx = max((i for i, c in enumerate(captions) if c.start - 0.05 <= t), default=0)
        self._live_caption = (clip_idx, cap_idx, t)
        row = self._caption_rows.get((clip_idx, cap_idx))
        table = self._captions_table
        if row is not None and row != table.currentRow() and table.state() != QAbstractItemView.State.EditingState:
            table.blockSignals(True)
            table.setCurrentCell(row, 2)
            table.blockSignals(False)
            table.scrollToItem(table.item(row, 2))
        self._render_caption_preview()

    def _reset_caption_player(self) -> None:
        self._caption_player.clear()
        self._caption_clip_idx = None
        self._caption_view_clip = 0
        self._live_caption = None

    def _refresh_caption_tabs(self) -> None:
        """One tab per transcribed clip (hidden when there's only one)."""
        tabs = self._caption_clip_tabs
        clips = self._project.clip_captions if self._project.captions_current() else []
        tabs.blockSignals(True)
        while tabs.count() > len(clips):
            tabs.removeTab(tabs.count() - 1)
        while tabs.count() < len(clips):
            tabs.addTab("")
        for i, captions in enumerate(clips):
            seg = self._project.segments[i] if i < len(self._project.segments) else None
            title = seg.label.strip() if seg else ""
            short = title if len(title) <= 22 else title[:21].rstrip() + "…"
            tabs.setTabText(i, f"{i + 1} · {short}" if short else f"Clip {i + 1}")
            n = len(captions)
            tabs.setTabToolTip(i, (f"{title} · " if title else "") + f"{n} caption{'s' if n != 1 else ''}")
        self._caption_view_clip = min(self._caption_view_clip, max(0, len(clips) - 1))
        if clips:
            tabs.setCurrentIndex(self._caption_view_clip)
        tabs.blockSignals(False)
        tabs.setVisible(len(clips) > 1)

    def _on_caption_clip_tab(self, index: int) -> None:
        if index >= 0:
            self._show_caption_clip(index)

    def _show_caption_clip(self, clip_idx: int) -> None:
        """Review one clip: its captions in the table, the clip itself in the preview."""
        if clip_idx != self._caption_clip_idx:
            self._caption_player.pause()
        self._caption_view_clip = clip_idx
        self._live_caption = None
        self._load_caption_clip(clip_idx)
        tabs = self._caption_clip_tabs
        if tabs.currentIndex() != clip_idx:
            tabs.blockSignals(True)
            tabs.setCurrentIndex(clip_idx)
            tabs.blockSignals(False)
        self._fill_captions_table(self._project.clip_captions)
        self._captions_status.setText(self._captions_status_text())
        self._render_caption_preview()

    def _captions_enabled(self) -> bool:
        return hasattr(self, "_auto_captions_check") and self._auto_captions_check.isChecked()

    def _on_captions_toggled(self, on: bool) -> None:
        settings().setValue(KEY_AUTO_CAPTIONS, bool(on))
        self._refresh_captions_step()
        self._update_nav()

    def _refresh_captions_step(self) -> None:
        if not hasattr(self, "_captions_table"):
            return
        enabled = self._captions_enabled()
        self._style_card.setEnabled(enabled)
        self._transcript_card.setEnabled(enabled)
        self._transcribe_btn.setEnabled(enabled and not self._busy and bool(self._project.clip_paths))
        self._transcribe_btn.setText("Transcribe again" if self._project.clip_captions else "Transcribe")
        editable = enabled and not self._busy and self._project.captions_current()
        self._insert_caption_btn.setEnabled(editable)
        self._delete_caption_btn.setEnabled(editable)

        if not self._project.captions_current():
            self._reset_caption_player()
            self._refresh_caption_tabs()
            self._fill_captions_table([])
        else:
            self._refresh_caption_tabs()
            self._fill_captions_table(self._project.clip_captions)
            if enabled and self._caption_clip_idx != self._caption_view_clip:
                self._load_caption_clip(self._caption_view_clip)  # show the first frame, ready to play

        self._captions_status.setText(self._captions_status_text())
        self._render_caption_preview()

    def _captions_status_text(self) -> str:
        if not self._captions_enabled():
            return "Captions are off. Continue to skip this step."
        if self._project.captions_current():
            clips = self._project.clip_captions
            view = min(self._caption_view_clip, len(clips) - 1)
            count = len(clips[view]) if clips else 0
            where = f"Clip {view + 1} of {len(clips)}: " if len(clips) > 1 else ""
            return (
                f"{where}{count} captions. Double-click to fix words or times. "
                "Missing words? Pause the clip where they're said and click ＋ Insert line."
            )
        if self._project.clip_captions:
            return "The clips changed since the last transcription. Continue to transcribe them again."
        return "Not transcribed yet. Continue transcribes the clips, then you can review the text here."

    def _fill_captions_table(self, clip_captions: list[list[Caption]]) -> None:
        table = self._captions_table
        table.blockSignals(True)
        table.setRowCount(0)
        self._caption_rows = {}
        for clip_idx, captions in enumerate(clip_captions):
            if clip_idx != self._caption_view_clip:
                continue  # one clip at a time
            for cap_idx, cap in enumerate(captions):
                row = table.rowCount()
                table.insertRow(row)
                clip_item = QTableWidgetItem(f"{clip_idx + 1}")
                time_item = QTableWidgetItem(f"{cap.start:5.1f}–{cap.end:.1f}s")
                text_item = QTableWidgetItem(cap.text)
                clip_item.setFlags(clip_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                for item in (clip_item, time_item):
                    item.setForeground(Qt.GlobalColor.gray)
                time_item.setToolTip("Double-click to change, e.g. 9.6–11.1")
                for item in (time_item, text_item):
                    item.setData(Qt.ItemDataRole.UserRole, (clip_idx, cap_idx))
                self._caption_rows[(clip_idx, cap_idx)] = row
                table.setItem(row, 0, clip_item)
                table.setItem(row, 1, time_item)
                table.setItem(row, 2, text_item)
        table.blockSignals(False)

    def _on_caption_edited(self, item: QTableWidgetItem) -> None:
        key = item.data(Qt.ItemDataRole.UserRole)
        if not key:
            return
        if item.column() == 1:
            self._on_caption_time_edited(key, item.text())
            return
        if item.column() != 2:
            return
        clip_idx, cap_idx = key
        captions = self._project.clip_captions[clip_idx]
        old = captions[cap_idx]
        text = item.text().strip()
        if text == old.text:
            return
        tokens = text.split()
        max_words = max(1, self._selected_caption_style().max_words)
        if len(tokens) <= max_words:
            captions[cap_idx] = Caption(old.start, old.end, text, old.words)
            self._render_caption_preview()
            return
        # Too many words for one caption (e.g. typed-in missing words): split like the
        # transcription does, sharing the line's time by word.
        step = (old.end - old.start) / len(tokens)
        parts = [
            Caption(old.start + k * step, old.start + min(len(tokens), k + max_words) * step, " ".join(tokens[k : k + max_words]))
            for k in range(0, len(tokens), max_words)
        ]
        captions[cap_idx : cap_idx + 1] = parts
        QTimer.singleShot(0, lambda: self._refresh_caption_rows((clip_idx, cap_idx)))

    def _on_caption_time_edited(self, key: tuple[int, int], text: str) -> None:
        clip_idx, cap_idx = key
        captions = self._project.clip_captions[clip_idx]
        cap = captions[cap_idx]
        match = _CAPTION_TIME_RE.match(text)
        low = captions[cap_idx - 1].end if cap_idx > 0 else 0.0
        high = captions[cap_idx + 1].start if cap_idx + 1 < len(captions) else self._clip_duration(clip_idx)
        start, end = (float(match.group(1)), float(match.group(2))) if match else (cap.start, cap.end)
        problem = None
        if not match:
            problem = "Type the time as start–end in seconds, e.g. 9.6–11.1."
        elif end - start < 0.2:
            problem = "The end must be after the start."
        elif start < low - 0.05 or end > high + 0.05:
            problem = f"That overlaps the next or previous caption. This one can use {low:.1f}–{high:.1f}s."
        if problem is None and (round(start, 2), round(end, 2)) != (round(cap.start, 2), round(cap.end, 2)):
            # Whisper's word timings no longer fit; the highlight is spread over the new span.
            captions[cap_idx] = Caption(start, end, cap.text)
        # Rewrite the cell (normalised, or reverted) after Qt finishes the edit.
        QTimer.singleShot(0, lambda: self._refresh_caption_rows(key))
        if problem:
            QMessageBox.warning(self, "Wordly", problem)

    def _clip_duration(self, clip_idx: int) -> float:
        if self._caption_clip_idx == clip_idx and self._caption_player.duration_seconds() > 0:
            return self._caption_player.duration_seconds()
        try:
            return ffprobe_duration_seconds(self._project.clip_paths[clip_idx])
        except Exception:  # noqa: BLE001 - unknown length: don't block the edit
            return float("inf")

    def _refresh_caption_rows(self, select: tuple[int, int] | None = None, *, edit_text: bool = False) -> None:
        if select and select[0] != self._caption_view_clip:
            self._show_caption_clip(select[0])
        self._refresh_caption_tabs()
        self._fill_captions_table(self._project.clip_captions)
        table = self._captions_table
        row = self._caption_rows.get(select) if select else None
        if row is not None:
            table.blockSignals(True)  # select without jumping playback
            table.setCurrentCell(row, 2)
            table.blockSignals(False)
            table.scrollToItem(table.item(row, 2))
            if edit_text:
                table.setFocus()
                table.editItem(table.item(row, 2))
        self._captions_status.setText(self._captions_status_text())
        self._render_caption_preview()
        self._update_nav()

    def _new_caption_slot(self) -> tuple[int, int, float, float] | None:
        """Where a new caption goes: the gap at the paused playhead, else after the selected line."""
        all_caps = self._project.clip_captions
        if self._caption_clip_idx is not None and self._caption_clip_idx < len(all_caps):
            clip_idx = self._caption_clip_idx
            caps = all_caps[clip_idx]
            t = self._caption_player.position_seconds()
            if not any(c.start <= t < c.end for c in caps):
                index = sum(1 for c in caps if c.start <= t)
                low = caps[index - 1].end if index > 0 else 0.0
                high = caps[index].start if index < len(caps) else self._clip_duration(clip_idx)
                start = max(low, t - 0.3)
                end = min(high, start + 3.0)
                if end - start >= 0.2:
                    return clip_idx, index, start, end
        item = self._captions_table.item(self._captions_table.currentRow(), 2)
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not key:
            view = self._caption_view_clip
            if view >= len(all_caps):
                return None
            if not all_caps[view]:
                return view, 0, 0.0, min(3.0, self._clip_duration(view))
            key = (view, len(all_caps[view]) - 1)
        clip_idx, cap_idx = key
        caps = all_caps[clip_idx]
        cur = caps[cap_idx]
        high = caps[cap_idx + 1].start if cap_idx + 1 < len(caps) else self._clip_duration(clip_idx)
        if high - cur.end >= 0.3:
            return clip_idx, cap_idx + 1, cur.end, min(high, cur.end + 3.0)
        # No gap after it: share the selected caption's time.
        mid = (cur.start + cur.end) / 2
        caps[cap_idx] = Caption(cur.start, mid, cur.text)
        return clip_idx, cap_idx + 1, mid, cur.end

    def _insert_caption_line(self) -> None:
        if not self._project.captions_current():
            return
        slot = self._new_caption_slot()
        if slot is None:
            return
        clip_idx, index, start, end = slot
        self._caption_player.pause()
        self._project.clip_captions[clip_idx].insert(index, Caption(start, end, ""))
        self._refresh_caption_rows((clip_idx, index), edit_text=True)

    def _delete_caption_line(self) -> None:
        item = self._captions_table.item(self._captions_table.currentRow(), 2)
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not key or not self._project.captions_current():
            return
        clip_idx, cap_idx = key
        captions = self._project.clip_captions[clip_idx]
        del captions[cap_idx]
        self._live_caption = None
        if captions:
            select = (clip_idx, min(cap_idx, len(captions) - 1))
        else:
            select = None
        self._refresh_caption_rows(select)

    def _start_transcription(self, *, advance_to: int | None = None) -> None:
        clips = [p for p in self._project.clip_paths if p.exists()]
        if not clips:
            QMessageBox.warning(self, "Wordly", "Cut the clips on the Highlights step first.")
            return
        source = list(self._project.clip_paths)
        max_words = self._selected_caption_style().max_words

        def job(*, progress_cb, should_cancel):
            durations = [ffprobe_duration_seconds(p) for p in clips]
            return transcribe_clips(
                clips,
                durations,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
                max_words=max_words,
            )

        def on_ok(result: list[list[Caption]]) -> None:
            self._project.clip_captions = [list(c) for c in result]
            self._project.captions_source = source
            count = sum(len(c) for c in result)
            log_step("captions", f"Transcribed {len(result)} clip(s) → {count} captions")
            self._refresh_captions_step()
            self._update_nav()
            if advance_to is not None:
                self._stack.setCurrentIndex(advance_to)

        def on_fail(message: str) -> None:
            self._refresh_captions_step()
            if message and message != "Cancelled":
                QMessageBox.warning(self, "Wordly", f"Transcription failed:\n{message}")

        self._run_job(job, on_ok=on_ok, on_fail=on_fail, message="Transcribing clips…")

    # --- Step 4: Verse & music ---------------------------------------------

    def _build_verse_music_step(self) -> QWidget:
        page, layout = self._page()
        row, (left, right) = self._columns(1, 1, breakpoint=700)
        layout.addWidget(row)

        verse_card = Card("Bible verse", "Shown above the video for the whole reel.")
        self._verse_ref_edit = QLineEdit()
        self._verse_ref_edit.setPlaceholderText("John 3:16")
        self._verse_ref_edit.textChanged.connect(self._sync_manual_verse)
        self._verse_edit = QPlainTextEdit()
        self._verse_edit.setPlaceholderText("For God so loved the world…")
        self._verse_edit.setMinimumHeight(170)
        self._verse_edit.textChanged.connect(self._sync_manual_verse)
        verse_card.body.addWidget(label("Reference", "FieldLabel"))
        verse_card.body.addWidget(self._verse_ref_edit)
        verse_card.body.addWidget(label("Verse text", "FieldLabel"))
        verse_card.body.addWidget(self._verse_edit)
        self._verse_lux_note = label("", "Notice", wrap=True)
        set_prop(self._verse_lux_note, "tone", "lux")
        self._verse_lux_note.setVisible(False)
        verse_card.body.addWidget(self._verse_lux_note)
        left.addWidget(verse_card)

        photo_card = Card(
            "End photo",
            f"Optional. Shown for the last {END_PHOTO_S:g} seconds with the music; the music ends with it.",
        )
        self._end_photo_url_edit = QLineEdit()
        self._end_photo_url_edit.setPlaceholderText("Facebook photo link")
        self._end_photo_url_edit.setClearButtonEnabled(True)
        self._end_photo_url_edit.returnPressed.connect(self._download_end_photo)
        self._end_photo_download_btn = button("Get photo", "Primary", on_click=self._download_end_photo)
        photo_url_row = QHBoxLayout()
        photo_url_row.setSpacing(8)
        photo_url_row.addWidget(self._end_photo_url_edit, 1)
        photo_url_row.addWidget(self._end_photo_download_btn)
        photo_card.body.addLayout(photo_url_row)
        self._end_photo_browse_btn = button("Choose image…", on_click=self._browse_end_photo)
        self._end_photo_remove_btn = button("Remove", "Ghost", on_click=lambda: self._set_end_photo(None))
        photo_actions = QHBoxLayout()
        photo_actions.addWidget(self._end_photo_browse_btn)
        photo_actions.addWidget(self._end_photo_remove_btn)
        photo_actions.addStretch(1)
        photo_card.body.addLayout(photo_actions)
        self._end_photo_preview = QLabel()
        self._end_photo_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        photo_card.body.addWidget(self._end_photo_preview)
        left.addWidget(photo_card)
        left.addStretch(1)
        self._show_end_photo(self._project.end_photo)

        music_card = Card("Instrumental", "Plays quietly under the whole reel.")
        # Library: everything in assets/music (downloads land there too).
        self._music_list = QListWidget()
        self._music_list.setMaximumHeight(230)
        self._music_list.currentItemChanged.connect(self._on_music_item_changed)
        self._music_empty = label(
            "No music yet. Download one below, or put audio files in assets/music.", "Faint", wrap=True
        )
        self._music_player = QMediaPlayer(self)
        self._music_player.setAudioOutput(QAudioOutput(self._music_player))
        self._music_player.playbackStateChanged.connect(self._on_music_playback_changed)
        self._listen_btn = button("▶  Listen", "Small", tooltip="Hear the selected song", on_click=self._toggle_music_listen)
        listen_row = QHBoxLayout()
        listen_row.addWidget(self._listen_btn)
        listen_row.addStretch(1)
        music_card.body.addWidget(label("Your music", "FieldLabel"))
        music_card.body.addWidget(self._music_list)
        music_card.body.addWidget(self._music_empty)
        music_card.body.addLayout(listen_row)
        self._music_url_edit = QLineEdit()
        self._music_url_edit.setPlaceholderText("YouTube link, or a song name to search")
        self._music_url_edit.setClearButtonEnabled(True)
        self._music_url_edit.returnPressed.connect(self._download_music_from_url)
        self._download_music_url_btn = button("Download", "Primary", on_click=self._download_music_from_url)
        url_row = QHBoxLayout()
        url_row.setSpacing(8)
        url_row.addWidget(self._music_url_edit, 1)
        url_row.addWidget(self._download_music_url_btn)
        music_card.body.addWidget(label("Find it online", "FieldLabel"))
        music_card.body.addLayout(url_row)
        music_card.body.addWidget(label("Already have the file?", "FieldLabel"))
        self._local_music_btn = button("Choose audio file…", on_click=self._browse_local_music)
        local_row = QHBoxLayout()
        local_row.addWidget(self._local_music_btn)
        local_row.addStretch(1)
        music_card.body.addLayout(local_row)
        self._music_status = label("No instrumental chosen yet.", "Notice", wrap=True)
        music_card.body.addWidget(self._music_status)
        right.addWidget(music_card)
        right.addStretch(1)
        self._refresh_music_library()
        return page

    # --- Step 5: Export ----------------------------------------------------

    def _build_export_step(self) -> QWidget:
        page, layout = self._page()

        self._export_result_card = Card("Project ready")
        set_prop(self._export_result_card, "tone", "success")
        self._export_result = label("", "Muted", wrap=True)
        self._export_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._export_result_card.body.addWidget(self._export_result)
        result_actions = QHBoxLayout()
        result_actions.setSpacing(8)
        self._open_resolve_btn = button(
            "Open in Resolve",
            "Primary",
            on_click=lambda: self._last_export_path and self._open_in_resolve(self._last_export_path),
        )
        reveal_btn = button(
            "Show in Finder" if platform.system() == "Darwin" else "Show folder",
            on_click=lambda: self._last_export_path and reveal_in_finder(self._last_export_path),
        )
        result_actions.addWidget(self._open_resolve_btn)
        result_actions.addWidget(reveal_btn)
        result_actions.addStretch(1)
        self._export_result_card.body.addLayout(result_actions)
        self._export_result_card.setVisible(False)
        layout.addWidget(self._export_result_card)

        row, (left, right) = self._columns(3, 2, breakpoint=760)
        layout.addWidget(row)

        summary_card = Card("What goes into the reel")
        self._summary_rows: dict[str, SummaryRow] = {}
        for key, title, step in (
            ("source", "Service", S_SOURCE),
            ("clips", "Clips", S_HIGHLIGHTS),
            ("captions", "Captions", S_CAPTIONS),
            ("verse", "Verse", S_VERSE_MUSIC),
            ("music", "Music", S_VERSE_MUSIC),
            ("end_photo", "End photo", S_VERSE_MUSIC),
        ):
            row_widget = SummaryRow(title)
            row_widget.edit_clicked.connect(lambda step=step: self._go_to_step(step))
            self._summary_rows[key] = row_widget
            summary_card.body.addWidget(row_widget)
        left.addWidget(summary_card)
        left.addStretch(1)

        project_card = Card("Resolve project")
        self._project_name_edit = QLineEdit(default_export_project_name())
        self._last_suggested_project_name = self._project_name_edit.text()
        self._project_name_edit.textChanged.connect(self._on_project_name_changed)
        project_card.body.addWidget(label("Project name", "FieldLabel"))
        project_card.body.addWidget(self._project_name_edit)
        self._export_path_label = label("", "Faint", wrap=True)
        project_card.body.addWidget(self._export_path_label)
        project_card.body.addSpacing(6)
        self._reel_layout_check = QCheckBox("Reel layout")
        self._reel_layout_check.setChecked(settings().value(KEY_REEL_LAYOUT, True, type=bool))
        self._reel_layout_check.toggled.connect(lambda on: settings().setValue(KEY_REEL_LAYOUT, bool(on)))
        project_card.body.addWidget(self._reel_layout_check)
        project_card.body.addWidget(
            label(
                "Video centred and framed on the speaker over a blurred copy of itself, "
                "verse above, captions below.",
                "Muted",
                wrap=True,
            )
        )
        self._export_template_note = label("", "Notice", wrap=True)
        set_prop(self._export_template_note, "tone", "warn")
        project_card.body.addWidget(self._export_template_note)
        right.addWidget(project_card)
        right.addWidget(
            label(
                "Wordly opens the project in DaVinci Resolve for you. From there, use Deliver to "
                "render the MP4. Resolve Free adds no watermark.",
                "Faint",
                wrap=True,
            )
        )
        right.addStretch(1)
        return page

    # --- Navigation --------------------------------------------------------

    def _clips_up_to_date(self) -> bool:
        valid = [p for p in self._project.clip_paths if p.exists()]
        return bool(valid) and len(valid) == len(self._project.segments)

    def _verse_music_ready(self) -> bool:
        verse = self._project.selected_verse
        music = self._project.selected_music
        return bool(
            verse
            and verse.reference.strip()
            and verse.text.strip()
            and music
            and music.local_path
            and music.local_path.is_file()
        )

    def _step_complete(self, idx: int) -> bool:
        """Whether the primary button may move on from step *idx*."""
        if idx == S_SOURCE:
            path = self._project.sermon_path
            return bool(path and path.exists())
        if idx == S_HIGHLIGHTS:
            return self._step_complete(S_SOURCE) and bool(self._project.segments)
        if idx == S_CAPTIONS:
            # Continue transcribes first when captions are on but not generated yet.
            return True
        if idx == S_VERSE_MUSIC:
            return self._verse_music_ready()
        if idx == S_EXPORT:
            return bool(self._project_name_edit.text().strip())
        return True

    def _step_done(self, idx: int) -> bool:
        """Whether step *idx* is finished, so later steps may be opened from the sidebar."""
        if idx == S_HIGHLIGHTS:
            return self._step_complete(S_HIGHLIGHTS) and self._clips_up_to_date()
        if idx == S_EXPORT:
            return self._last_export_path is not None
        return self._step_complete(idx)

    def _step_reachable(self, idx: int) -> bool:
        return all(self._step_done(i) for i in range(idx))

    def _incomplete_hint(self, idx: int) -> str:
        if idx == S_SOURCE:
            return "Load a service video to continue"
        if idx == S_HIGHLIGHTS:
            if not self._step_complete(S_SOURCE):
                return "Load a service video first"
            return "Add at least one clip"
        if idx == S_VERSE_MUSIC:
            verse = self._project.selected_verse
            if not verse:
                return "Add the verse reference and text"
            return "Choose an instrumental"
        if idx == S_EXPORT:
            return "Name the project"
        return ""

    def _step_summary(self, idx: int) -> str:
        p = self._project
        if idx == S_SOURCE:
            if not p.sermon_path:
                return "Link or video file"
            if p.sermon_duration_s > 0:
                return f"{p.sermon_path.name} · {_short_clock(p.sermon_duration_s)}"
            return p.sermon_path.name
        if idx == S_HIGHLIGHTS:
            if not p.segments:
                return "Choose 2–3 moments"
            total = self._segments_total_s()
            count = len(p.segments)
            return f"{count} clip{'s' if count != 1 else ''} · {total:.0f}s"
        if idx == S_CAPTIONS:
            if not self._captions_enabled():
                return "Off"
            if p.captions_current():
                return f"{sum(len(c) for c in p.clip_captions)} captions · {self._caption_style_combo.currentText()}"
            return f"On · {self._caption_style_combo.currentText()}"
        if idx == S_VERSE_MUSIC:
            parts = []
            if p.selected_verse:
                parts.append(p.selected_verse.reference)
            if p.selected_music:
                parts.append(p.selected_music.title)
            return " · ".join(parts) or "Verse and instrumental"
        if idx == S_EXPORT:
            if self._last_export_path is not None:
                return f"Exported · {self._last_export_path.stem}"
            return self._project_name_edit.text().strip() or "Resolve project"
        return ""

    def _primary_label(self, idx: int) -> str:
        if self._busy:
            return "Working…"
        if idx == S_HIGHLIGHTS and not self._clips_up_to_date():
            return "Cut clips && continue →"
        if idx == S_CAPTIONS and self._captions_enabled() and not self._project.captions_current():
            return "Transcribe && continue →"
        if idx == S_EXPORT:
            return "Export again" if self._last_export_path is not None else "Generate Resolve project"
        return "Continue →"

    def _update_nav(self) -> None:
        idx = self._stack.currentIndex()
        self._back_btn.setVisible(idx > 0)
        self._back_btn.setEnabled(not self._busy)
        complete = self._step_complete(idx)
        self._next_btn.setEnabled(not self._busy and complete)
        self._next_btn.setText(self._primary_label(idx))
        self._nav_hint.setText("" if complete or self._busy else self._incomplete_hint(idx))
        self._job_bar.setVisible(self._busy)
        self._cancel_btn.setVisible(self._busy and self._worker is not None)
        for btn in (
            self._download_btn,
            self._open_local_btn,
            self._find_highlights_btn,
            self._download_music_url_btn,
            self._local_music_btn,
            self._end_photo_download_btn,
            self._end_photo_browse_btn,
            self._open_resolve_btn,
        ):
            btn.setEnabled(not self._busy)
        self._find_highlights_btn.setEnabled(not self._busy and self._step_complete(S_SOURCE))
        self._find_sermon_btn.setEnabled(not self._busy and self._step_complete(S_SOURCE))
        self._paste_times_btn.setEnabled(not self._busy and self._step_complete(S_SOURCE))
        self._save_clips_btn.setEnabled(not self._busy and self._step_complete(S_HIGHLIGHTS))
        self._transcribe_btn.setEnabled(
            self._captions_enabled() and not self._busy and bool(self._project.clip_paths)
        )
        self._refresh_sidebar()
        self._refresh_add_segment_enabled()
        if idx == S_EXPORT:
            self._refresh_export_page()

    def _refresh_sidebar(self) -> None:
        current = self._stack.currentIndex()
        for i, item in enumerate(self._sidebar.items):
            reachable = self._step_reachable(i)
            if i == current:
                state = "current"
            elif self._step_done(i) and reachable:
                state = "done"
            elif reachable:
                state = "todo"
            else:
                state = "locked"
            item.update_state(state, self._step_summary(i), reachable and i != current and not self._busy)
        if self._project.sermon_path:
            self._sidebar.footer.setText(f"Project “{self._project_name_edit.text().strip() or '—'}”")
        else:
            self._sidebar.footer.setText("Start with a service video.")

    def _on_step_changed(self, index: int) -> None:
        if self._last_step_index == S_HIGHLIGHTS and index != S_HIGHLIGHTS:
            self._preview.stop()
        if self._last_step_index == S_CAPTIONS and index != S_CAPTIONS:
            self._caption_player.stop()
        if self._last_step_index == S_VERSE_MUSIC and index != S_VERSE_MUSIC:
            self._music_player.stop()
        if index == S_VERSE_MUSIC:
            self._refresh_music_library()  # pick up files added to assets/music meanwhile
        self._last_step_index = index
        _, title, subtitle = _STEPS[index]
        self._page_eyebrow.setText(f"STEP {index + 1} OF {len(_STEPS)}")
        self._page_title.setText(title)
        self._page_subtitle.setText(subtitle)
        if index == S_HIGHLIGHTS:
            self._refresh_timestamp_hints()
        elif index == S_CAPTIONS:
            self._refresh_captions_step()
        elif index == S_EXPORT:
            self._refresh_default_project_name()
        self._render_caption_preview()
        self._stack.update()
        self._request_full_repaint()
        self._update_nav()

    def _go_to_step(self, index: int) -> None:
        if self._busy or index == self._stack.currentIndex():
            return
        if index > self._stack.currentIndex() and not self._step_reachable(index):
            return
        self._stack.setCurrentIndex(index)

    def _on_project_name_changed(self) -> None:
        self._update_nav()

    def _refresh_default_project_name(self) -> None:
        suggested = default_export_project_name()
        current = self._project_name_edit.text().strip()
        if not current or current == self._last_suggested_project_name:
            self._project_name_edit.setText(suggested)
        self._last_suggested_project_name = suggested

    def _segments_total_s(self) -> float:
        total = 0.0
        for seg in self._project.segments:
            try:
                total += _seconds(seg.end_text) - _seconds(seg.start_text)
            except ValueError:
                pass
        return total

    def _refresh_export_page(self) -> None:
        p = self._project
        rows = self._summary_rows
        if p.sermon_path:
            rows["source"].set_value(self._step_summary(S_SOURCE))
        else:
            rows["source"].set_value("No service loaded", "todo")
        if p.segments:
            ready = "cut and ready" if self._clips_up_to_date() else "will be cut during export"
            rows["clips"].set_value(f"{self._step_summary(S_HIGHLIGHTS)} — {ready}")
        else:
            rows["clips"].set_value("No clips", "todo")
        if not self._captions_enabled():
            rows["captions"].set_value("Off", "off")
        elif p.captions_current():
            count = sum(len(c) for c in p.clip_captions)
            rows["captions"].set_value(f"{count} reviewed captions, “{self._selected_caption_style().name}” look")
        else:
            rows["captions"].set_value(
                f"“{self._selected_caption_style().name}” look — clips are transcribed during export"
            )
        verse = p.selected_verse
        if verse:
            preview = verse.text if len(verse.text) <= 90 else verse.text[:88].rstrip() + "…"
            rows["verse"].set_value(f"{verse.reference} — {preview}")
        else:
            rows["verse"].set_value("Missing", "todo")
        if p.selected_music and p.selected_music.local_path:
            rows["music"].set_value(p.selected_music.title)
        else:
            rows["music"].set_value("Missing", "todo")
        if p.end_photo is not None:
            rows["end_photo"].set_value(f"{p.end_photo.name} · last {END_PHOTO_S:g} s")
        else:
            rows["end_photo"].set_value("None", "off")

        name = self._project_name_edit.text().strip() or default_export_project_name()
        self._export_path_label.setText(f"Saved to exports/{name}/{name}.fcpxml")
        if template_available():
            self._export_template_note.setVisible(False)
        else:
            self._export_template_note.setText(
                "Template missing: add assets/resolve_templates/sermon-highlights.fcpxml "
                "(or set WORDLY_RESOLVE_TEMPLATE). See docs/TEMPLATE_AUTHORING.md."
            )
            self._export_template_note.setVisible(True)

    def _go_back(self) -> None:
        if self._busy:
            return
        if self._stack.currentIndex() > 0:
            self._stack.setCurrentIndex(self._stack.currentIndex() - 1)

    def _primary_shortcut(self) -> None:
        if self._next_btn.isEnabled():
            self._go_next()

    def _go_next(self) -> None:
        idx = self._stack.currentIndex()
        if not self._step_complete(idx):
            return
        if idx == S_HIGHLIGHTS:
            self._preview.stop()
            if self._clips_up_to_date():
                self._stack.setCurrentIndex(S_CAPTIONS)
            else:
                self._start_trim_clips(advance_to=S_CAPTIONS)
            return
        if idx == S_CAPTIONS and self._captions_enabled() and not self._project.captions_current():
            self._start_transcription(advance_to=S_VERSE_MUSIC)
            return
        if idx >= len(self._steps) - 1:
            self._generate_resolve()
            return
        self._stack.setCurrentIndex(idx + 1)

    # --- Source ------------------------------------------------------------

    def _restore_prefs(self) -> None:
        s = settings()
        self._url_edit.setText(s.value(KEY_LAST_FB_URL, "", type=str))

    def _save_prefs(self) -> None:
        s = settings()
        s.setValue(KEY_LAST_FB_URL, self._url_edit.text().strip())

    def _open_local_sermon(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open sermon video",
            str(DOWNLOADS),
            "Video (*.mp4 *.mkv *.mov *.webm *.m4v);;All files (*)",
        )
        if not path:
            return
        self._set_sermon(Path(path))

    def _set_sermon(self, path: Path) -> None:
        """Set the active sermon path and probe its duration off the UI thread."""
        resolved = path.resolve()
        if resolved != self._project.sermon_path:
            self._project.clip_paths = []
            self._project.joined_clip_path = None
        self._project.sermon_path = resolved
        self._project.sermon_duration_s = 0.0
        self._source_loaded_title.setText(f"✓  {resolved.name}")
        self._download_result.setText("Reading video length…")
        self._source_loaded_card.setVisible(True)
        # Start the preview immediately; the player can buffer while ffprobe runs.
        self._load_preview_for_current_sermon()
        if self._stack.currentIndex() == S_SOURCE:
            self._stack.setCurrentIndex(S_HIGHLIGHTS)
        self._update_nav()

        def _probe() -> None:
            try:
                dur = ffprobe_duration_seconds(resolved)
                self._sermon_probed.emit(dur)
            except Exception as exc:  # noqa: BLE001
                self._sermon_probe_failed.emit(str(exc))

        threading.Thread(target=_probe, daemon=True).start()

    @Slot(float)
    def _on_preview_duration(self, duration_s: float) -> None:
        if duration_s > 0:
            self._project.sermon_duration_s = duration_s
            self._refresh_timestamp_hints()

    @Slot(float)
    def _on_sermon_probed(self, duration: float) -> None:
        self._project.sermon_duration_s = duration
        path = self._project.sermon_path
        if path:
            self._download_result.setText(f"{_short_clock(duration)} long · {path.parent}")
            self._preview.set_duration_seconds(duration)
        self._refresh_timestamp_hints()
        self._update_nav()

    @Slot(str)
    def _on_sermon_probe_failed(self, msg: str) -> None:
        self._download_result.setText("Couldn't read the video length.")
        QMessageBox.warning(self, "Wordly", msg)

    def _start_download(self) -> None:
        url = self._url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "Wordly", "Paste a Facebook or YouTube link first.")
            return
        self._save_prefs()
        self._project.fb_url = url

        def job(*, progress_cb, should_cancel):
            return download_facebook_video(
                url,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
            )

        self._run_job(job, on_ok=lambda path: self._set_sermon(path), message="Downloading the service…")

    # --- Highlights --------------------------------------------------------

    def _refresh_timestamp_hints(self) -> None:
        if self._project.sermon_duration_s > 0:
            self._seg_duration_hint.setText(f"Service {_short_clock(self._project.sermon_duration_s)}")
        elif self._project.sermon_path:
            self._seg_duration_hint.setText("Loading…")
        else:
            self._seg_duration_hint.setText("No video")
        self._on_timestamp_fields_changed()

    def _field_time_error(self, text: str) -> str | None:
        raw = text.strip()
        if not raw:
            return None
        try:
            parse_timecode(raw)
        except ValueError as exc:
            return str(exc)
        return None

    def _on_timestamp_fields_changed(self) -> None:
        start_t = self._seg_start.text().strip()
        end_t = self._seg_end.text().strip()
        start_err = self._field_time_error(start_t)
        end_err = self._field_time_error(end_t)
        set_prop(self._seg_start, "invalid", "true" if start_err else "false")
        set_prop(self._seg_end, "invalid", "true" if end_err else "false")
        error = None
        if start_err:
            error = f"Start: {start_err}"
        elif end_err:
            error = f"End: {end_err}"
        elif start_t and end_t:
            try:
                media = self._project.sermon_duration_s or None
                validate_segment_times(start_t, end_t, media_duration_s=media)
            except ValueError as exc:
                error = str(exc)
        self._seg_error.setText(error or "")
        self._seg_error.setVisible(bool(error))
        self._refresh_add_segment_enabled()

    def _segment_form_valid(self) -> bool:
        start_t = self._seg_start.text().strip()
        end_t = self._seg_end.text().strip()
        if not start_t or not end_t:
            return False
        if self._field_time_error(start_t) or self._field_time_error(end_t):
            return False
        try:
            media = self._project.sermon_duration_s or None
            validate_segment_times(start_t, end_t, media_duration_s=media)
        except ValueError:
            return False
        return True

    def _refresh_add_segment_enabled(self) -> None:
        valid = self._segment_form_valid() and not self._busy
        self._add_segment_btn.setEnabled(valid)
        self._preview_range_btn.setEnabled(valid and self._project.sermon_path is not None)

    def _mark_from_playhead(self, field: QLineEdit) -> None:
        if not self._project.sermon_path:
            return
        field.setText(format_timecode(round(self._preview.position_seconds(), 1)))

    def _preview_typed_range(self) -> None:
        if not self._segment_form_valid():
            return
        start_ms = int(_seconds(self._seg_start.text()) * 1000)
        end_ms = int(_seconds(self._seg_end.text()) * 1000)
        self._preview.set_trim_window_ms(start_ms, end_ms)
        self._preview.play()

    def _ensure_api_key(self) -> bool:
        """Ask once for the Anthropic API key and save it to the project's .env file."""
        try:
            api_key()
            return True
        except MissingApiKey:
            pass
        key, ok = QInputDialog.getText(
            self,
            "Anthropic API key",
            "Paste your Anthropic API key (from console.anthropic.com).\n"
            "Wordly saves it in this project's .env file (git-ignored, only readable by you).",
            QLineEdit.EchoMode.Password,
        )
        if not ok or not key.strip():
            return False
        try:
            save_api_key(key)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Wordly", str(exc))
            return False
        return True

    def _find_sermon(self) -> None:
        sermon = self._project.sermon_path
        if not sermon or not sermon.is_file():
            QMessageBox.warning(self, "Wordly", "Load the service video first.")
            return
        if self._project.segments:
            answer = QMessageBox.question(
                self,
                "Wordly",
                "Find sermon replaces your clips with one clip around the sermon. Continue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._preview.stop()

        def job(*, progress_cb, should_cancel):
            return find_sermon(sermon, progress_cb=progress_cb, should_cancel=should_cancel)

        def on_ok(bounds: tuple[float, float]) -> None:
            start, end = bounds
            self._project.segments = [ClipSegment(highlight_timecode(start), highlight_timecode(end), "Sermon")]
            self._invalidate_clips()
            self._refresh_segment_list()
            self._select_segment(0)
            self._highlights_summary.setText(
                f"<b>Sermon found: {format_clock(start)}–{format_clock(end)}</b> ({_short_clock(end - start)}). "
                "Check the start and end on the player — the end can run into the closing prayer — "
                "then let Lux pick highlights from it."
            )
            self._highlights_summary.setVisible(True)
            self._on_timestamp_fields_changed()
            self._update_nav()

        def on_fail(message: str) -> None:
            if message and message != "Cancelled":
                QMessageBox.warning(self, "Wordly", f"Couldn't find the sermon:\n{message}")

        self._run_job(
            job,
            on_ok=on_ok,
            on_fail=on_fail,
            message="Listening for the sermon — a few minutes the first time…",
        )

    def _paste_timestamps(self) -> None:
        """Turn pasted start times into 60 s clips, then let Lux scan just those."""
        sermon = self._project.sermon_path
        if not sermon or not sermon.is_file():
            QMessageBox.warning(self, "Wordly", "Load the service video first.")
            return
        text, ok = QInputDialog.getMultiLineText(
            self,
            "Paste times",
            "One start time per line. 101 means 1:01:00; 1:49:30 is exact.\n"
            "Each becomes a 60-second clip, and Lux picks highlights from inside them.",
        )
        if not ok or not text.strip():
            return
        try:
            windows = parse_timestamp_list(text, length_s=60.0, media_duration_s=self._project.sermon_duration_s or None)
        except ValueError as exc:
            QMessageBox.warning(self, "Wordly", str(exc))
            return
        self._project.segments = [
            ClipSegment(highlight_timecode(start), highlight_timecode(end)) for start, end in windows
        ]
        self._invalidate_clips()
        self._refresh_segment_list()
        self._on_segment_row_changed(-1)
        self._update_nav()
        self._find_highlights(confirm=False)

    def _find_highlights(self, *, confirm: bool = True) -> None:
        sermon = self._project.sermon_path
        if not sermon or not sermon.is_file():
            QMessageBox.warning(self, "Wordly", "Load the service video first.")
            return
        # Trimmed clips mark where Lux should look; without any it scans the whole video.
        ranges: list[tuple[float, float]] = []
        for seg in self._project.segments:
            try:
                ranges.append((_seconds(seg.start_text), _seconds(seg.end_text)))
            except ValueError:
                continue
        if ranges and confirm:
            total = sum(b - a for a, b in ranges)
            parts = "this part" if len(ranges) == 1 else f"these {len(ranges)} parts"
            answer = QMessageBox.question(
                self,
                "Wordly",
                f"Lux will look for highlights only inside your trimmed clips "
                f"({_short_clock(total)} in total) and replace {parts} with its picks.\n\n"
                "To scan the whole video instead, remove the clips first.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not self._ensure_api_key():
            return
        self._preview.stop()

        def job(*, progress_cb, should_cancel):
            return find_highlights(
                sermon,
                ranges=ranges or None,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
                ledger=LUX_LEDGER,
            )

        def on_fail(message: str) -> None:
            self._refresh_lux_spend()  # a failed answer is still billed
            if message and message != "Cancelled":
                QMessageBox.warning(self, "Wordly", f"Couldn't find highlights:\n{message}")

        self._run_job(
            job,
            on_ok=self._apply_highlights,
            on_fail=on_fail,
            message=(
                "Lux is listening to your trimmed part…"
                if ranges
                else "Lux is listening to the whole service — a few minutes the first time…"
            ),
        )

    def _refresh_lux_spend(self) -> None:
        self._lux_spend.setText(usage_totals(LUX_LEDGER).describe())

    def _apply_highlights(self, plan: HighlightPlan) -> None:
        self._refresh_lux_spend()
        self._project.segments = [
            ClipSegment(highlight_timecode(h.start), highlight_timecode(h.end), h.title) for h in plan.highlights
        ]
        self._invalidate_clips()
        self._refresh_segment_list()
        self._on_segment_row_changed(-1)
        if plan.verse_reference and not (self._verse_ref_edit.text().strip() or self._verse_edit.toPlainText().strip()):
            # Pre-fill the verse step; the wording is from Lux, so it should be checked.
            self._verse_ref_edit.setText(plan.verse_reference)
            self._verse_edit.setPlainText(plan.verse_text)
            reason = f" {plan.verse_reason}." if plan.verse_reason else ""
            self._verse_lux_note.setText(
                f"✦ Suggested by Lux.{reason} Check the wording against your Bible translation."
            )
            self._verse_lux_note.setVisible(True)
        lines = []
        sermon = f"Lux scanned {format_clock(plan.sermon_start)}–{format_clock(plan.sermon_end)}"
        lines.append(f"<b>{sermon}</b>" + (f" · “{plan.sermon_title}”" if plan.sermon_title else ""))
        for i, h in enumerate(plan.highlights, start=1):
            lines.append(f"{i}. <b>{h.title}</b> — {h.reason}")
        lines.append(f"Total {plan.total_s:.0f}s.")
        if plan.verse_reference:
            lines.append(f"Verse: {plan.verse_reference}")
        lines.extend(plan.notes)
        if plan.usage is not None:
            lines.append(f"<span style='opacity:0.75'>Lux used {plan.usage.describe()}</span>")
        self._highlights_summary.setText("<br>".join(lines))
        self._highlights_summary.setVisible(True)
        self._on_timestamp_fields_changed()
        self._update_nav()

    def _invalidate_clips(self) -> None:
        self._project.clip_paths = []
        self._project.joined_clip_path = None

    def _refresh_segment_list(self, select: int | None = None) -> None:
        lst = self._segment_list
        lst.blockSignals(True)
        lst.clear()
        for i, seg in enumerate(self._project.segments, start=1):
            try:
                start_s, end_s = _seconds(seg.start_text), _seconds(seg.end_text)
                meta = f"{_short_clock(start_s)} → {_short_clock(end_s)} · {end_s - start_s:.0f}s"
            except ValueError:
                meta = f"{seg.start_text} → {seg.end_text}"
            title = seg.label.strip() or f"Clip {i}"
            row = ClipRow(i, title, meta)
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 50))
            lst.addItem(item)
            lst.setItemWidget(item, row)
        lst.setCurrentRow(select if select is not None else -1)
        lst.blockSignals(False)

        has = bool(self._project.segments)
        lst.setVisible(has)
        self._clips_empty.setVisible(not has)
        self._remove_seg_btn.setEnabled(select is not None)
        total = self._segments_total_s()
        if has:
            self._clips_total_pill.setText(f"{total:.0f}s / {MAX_TOTAL_S:.0f}s")
            set_prop(self._clips_total_pill, "tone", "warn" if total > MAX_TOTAL_S else "ok")
            self._clips_total_pill.setToolTip(
                "Reels work best under 2 minutes." if total > MAX_TOTAL_S else "Total length of all clips."
            )
        else:
            self._clips_total_pill.setText("")
        self._clips_total_pill.setVisible(has)
        self._set_composer_mode(select)

    def _set_composer_mode(self, row: int | None) -> None:
        self._editing_row = row
        if row is None:
            self._composer_card.set_title("Mark a clip")
            self._add_segment_btn.setText("Add clip")
            self._cancel_edit_btn.setVisible(False)
        else:
            self._composer_card.set_title(f"Edit clip {row + 1}")
            self._add_segment_btn.setText("Save changes")
            self._cancel_edit_btn.setVisible(True)

    def _select_segment(self, row: int | None) -> None:
        self._segment_list.setCurrentRow(-1 if row is None else row)
        if row is None:
            self._on_segment_row_changed(-1)

    def _on_segment_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._project.segments):
            self._set_composer_mode(None)
            self._remove_seg_btn.setEnabled(False)
            for field in (self._seg_start, self._seg_end, self._seg_label):
                field.clear()
            self._preview.clear_trim_window()
            return
        seg = self._project.segments[row]
        self._set_composer_mode(row)
        self._remove_seg_btn.setEnabled(True)
        self._seg_start.setText(seg.start_text)
        self._seg_end.setText(seg.end_text)
        self._seg_label.setText(seg.label)
        self._sync_preview_segment()

    def _set_segment_end_offset(self, offset_s: float) -> None:
        start_t = normalize_four_digit_timecode(self._seg_start.text().strip())
        if not start_t:
            self._seg_error.setText("Enter a start time first.")
            self._seg_error.setVisible(True)
            return
        if start_t != self._seg_start.text().strip():
            self._seg_start.setText(start_t)
        try:
            media = self._project.sermon_duration_s or None
            end_t = end_timecode_from_start_offset(
                start_t,
                offset_s,
                media_duration_s=media,
            )
        except ValueError as exc:
            self._seg_error.setText(str(exc))
            self._seg_error.setVisible(True)
            return
        self._seg_end.setText(end_t)
        self._on_timestamp_fields_changed()

    def _add_segment(self) -> None:
        if not self._segment_form_valid():
            return
        seg = ClipSegment(self._seg_start.text().strip(), self._seg_end.text().strip(), self._seg_label.text().strip())
        if self._editing_row is not None and self._editing_row < len(self._project.segments):
            self._project.segments[self._editing_row] = seg
        else:
            self._project.segments.append(seg)
        self._invalidate_clips()
        self._refresh_segment_list()
        self._on_segment_row_changed(-1)
        self._update_nav()

    def _remove_segment(self) -> None:
        row = self._segment_list.currentRow()
        if row < 0 or row >= len(self._project.segments):
            return
        del self._project.segments[row]
        self._invalidate_clips()
        self._refresh_segment_list()
        self._on_segment_row_changed(-1)
        self._update_nav()

    def _load_preview_for_current_sermon(self) -> None:
        path = self._project.sermon_path
        if not path:
            self._preview.clear()
            return
        self._preview.load_file(path)
        self._sync_preview_segment()

    def _sync_preview_segment(self) -> None:
        row = self._segment_list.currentRow()
        if row < 0 or row >= len(self._project.segments):
            return
        seg = self._project.segments[row]
        try:
            start_ms = int(_seconds(seg.start_text) * 1000)
            end_ms = int(_seconds(seg.end_text) * 1000)
        except ValueError:
            return
        self._preview.set_trim_window_ms(start_ms, end_ms)
        self._preview.seek_trim_start()

    def _start_trim_clips(self, *, advance_to: int | None = None) -> None:
        if not self._project.sermon_path or not self._project.segments:
            return
        sermon = self._project.sermon_path
        segments = list(self._project.segments)
        stem = sermon.stem[:40]
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)
        output_dir = CLIPS / safe

        def job(*, progress_cb, should_cancel):
            return export_clips(
                sermon,
                segments,
                output_dir,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
            )

        def on_ok(paths: list[Path]) -> None:
            self._project.clip_paths = paths
            self._project.joined_clip_path = paths[0] if paths else None
            self._project.clip_captions = []
            self._project.captions_source = []
            self._update_nav()
            if advance_to is not None:
                self._stack.setCurrentIndex(advance_to)

        self._run_job(job, on_ok=on_ok, on_fail=lambda _err: None, message="Cutting clips…")

    def _export_trimmed_clips_clicked(self) -> None:
        if not self._project.sermon_path or not self._project.segments:
            QMessageBox.warning(self, "Wordly", "Add clips first.")
            return

        dest = QFileDialog.getExistingDirectory(
            self,
            "Choose folder for the clips",
            str(DOWNLOADS),
        )
        if not dest:
            return
        output_dir = Path(dest).resolve()
        sermon = self._project.sermon_path
        segments = list(self._project.segments)

        def job(*, progress_cb, should_cancel):
            return export_clips(
                sermon,
                segments,
                output_dir,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
            )

        def on_ok(paths: list[Path]) -> None:
            QMessageBox.information(
                self,
                "Wordly",
                f"Saved {len(paths)} clip(s) to:\n{output_dir}",
            )

        self._run_job(job, on_ok=on_ok, message="Saving clips…")

    def _export_merged_video_clicked(self) -> None:
        if not self._project.sermon_path or not self._project.segments:
            QMessageBox.warning(self, "Wordly", "Add clips first.")
            return

        suggested = (self._project.project_name or "highlights")[:60].strip() or "highlights"
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in suggested)
        default_path = (DOWNLOADS / f"{safe}.mp4").resolve()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save merged video",
            str(default_path),
            "Video (*.mp4);;All files (*)",
        )
        if not path:
            return
        out_path = Path(path).resolve()
        if out_path.suffix.lower() != ".mp4":
            out_path = out_path.with_suffix(".mp4")

        sermon = self._project.sermon_path
        segments = list(self._project.segments)
        tmp_dir = CLIPS / f"{safe}_export"

        def job(*, progress_cb, should_cancel):
            clips = export_clips(
                sermon,
                segments,
                tmp_dir,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
            )
            return merge_clips(
                clips,
                out_path,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
            )

        def on_ok(merged: Path) -> None:
            QMessageBox.information(self, "Wordly", f"Saved merged video:\n{merged}")

        self._run_job(job, on_ok=on_ok, message="Saving merged video…")

    # --- Verse & music -----------------------------------------------------

    def _sync_manual_verse(self) -> None:
        reference = self._verse_ref_edit.text().strip()
        text = self._verse_edit.toPlainText().strip()
        lines = text.splitlines()
        if not reference and len(lines) > 1 and _VERSE_REFERENCE_RE.match(lines[0].strip()):
            # "John 3:16" pasted on the first line of the text box: move it to Reference.
            reference = lines[0].strip()
            text = "\n".join(lines[1:]).strip()
            self._verse_ref_edit.blockSignals(True)
            self._verse_edit.blockSignals(True)
            self._verse_ref_edit.setText(reference)
            self._verse_edit.setPlainText(text)
            self._verse_ref_edit.blockSignals(False)
            self._verse_edit.blockSignals(False)
        self._project.selected_verse = VerseChoice(reference, text) if reference and text else None
        self._update_nav()

    def _download_music_from_url(self) -> None:
        query = self._music_url_edit.text().strip()
        if not query:
            QMessageBox.warning(self, "Wordly", "Paste a YouTube link or type a song name first.")
            return
        self._run_instrumental_download(query, title=query[:80])

    def _run_instrumental_download(self, query: str, *, title: str) -> None:
        def job(*, progress_cb, should_cancel):
            return download_instrumental(query, progress_cb=progress_cb, should_cancel=should_cancel)

        def on_ok(path: Path) -> None:
            self._refresh_music_library()
            self._set_music(MusicChoice(title=track_title(path) or title, local_path=path))

        self._run_job(job, on_ok=on_ok, message="Downloading the instrumental…")

    def _browse_local_music(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose instrumental audio",
            str(DOWNLOADS),
            "Audio (*.mp3 *.m4a *.opus *.webm *.ogg *.aac *.wav);;All files (*)",
        )
        if not path:
            return
        p = Path(path).resolve()
        if p.suffix.lower() not in AUDIO_EXTENSIONS:
            QMessageBox.warning(self, "Wordly", "Please choose a supported audio file.")
            return
        self._set_music(MusicChoice(title=p.stem, local_path=p))

    def _download_end_photo(self) -> None:
        url = self._end_photo_url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "Wordly", "Paste the Facebook photo link first.")
            return

        def job(*, progress_cb, should_cancel):
            progress_cb(-1.0, "Getting the photo…")
            return download_photo(url)

        def on_fail(message: str) -> None:
            if message and message != "Cancelled":
                QMessageBox.warning(self, "Wordly", f"Couldn't get the photo:\n{message}")

        self._run_job(job, on_ok=self._set_end_photo, on_fail=on_fail, message="Getting the photo…")

    def _browse_end_photo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose the end photo",
            str(DOWNLOADS),
            "Images (*.jpg *.jpeg *.png *.webp);;All files (*)",
        )
        if not path:
            return
        p = Path(path).resolve()
        if p.suffix.lower() not in IMAGE_EXTENSIONS:
            QMessageBox.warning(self, "Wordly", "Please choose a JPG, PNG or WebP image.")
            return
        self._set_end_photo(p)

    def _set_end_photo(self, path: Path | None) -> None:
        self._project.end_photo = path
        self._show_end_photo(path)
        self._update_nav()

    def _show_end_photo(self, path: Path | None) -> None:
        pixmap = QPixmap(str(path)) if path is not None else QPixmap()
        if path is not None and not pixmap.isNull():
            self._end_photo_preview.setPixmap(
                pixmap.scaled(QSize(220, 220), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            )
        else:
            self._end_photo_preview.clear()
        self._end_photo_preview.setVisible(path is not None)
        self._end_photo_remove_btn.setVisible(path is not None)

    def _set_music(self, music: MusicChoice) -> None:
        self._project.selected_music = music
        self._music_status.setText(f"♪  {music.title}")
        set_prop(self._music_status, "tone", "ok")
        self._select_music_row(music.local_path)
        self._update_nav()

    def _refresh_music_library(self) -> None:
        """List assets/music; the chosen song stays selected."""
        lst = self._music_list
        lst.blockSignals(True)
        lst.clear()
        for path in library_tracks():
            item = QListWidgetItem(f"♪  {track_title(path)}")
            item.setData(Qt.ItemDataRole.UserRole, str(path.resolve()))
            item.setToolTip(path.name)
            lst.addItem(item)
        lst.blockSignals(False)
        has = lst.count() > 0
        lst.setVisible(has)
        self._music_empty.setVisible(not has)
        self._listen_btn.setVisible(has)
        music = self._project.selected_music
        self._select_music_row(music.local_path if music else None)

    def _select_music_row(self, path: Path | None) -> None:
        lst = self._music_list
        target = str(Path(path).resolve()) if path else None
        row = next((i for i in range(lst.count()) if lst.item(i).data(Qt.ItemDataRole.UserRole) == target), -1)
        lst.blockSignals(True)
        lst.setCurrentRow(row)
        lst.blockSignals(False)

    def _on_music_item_changed(self, item: QListWidgetItem | None, _previous=None) -> None:
        if item is None:
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        if not path.is_file():
            self._refresh_music_library()  # removed from the folder since it was listed
            return
        if self._music_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._play_music(path)  # keep listening, now to the new pick
        self._set_music(MusicChoice(title=track_title(path), local_path=path))

    def _play_music(self, path: Path) -> None:
        self._music_player.setSource(QUrl.fromLocalFile(str(path)))
        self._music_player.play()

    def _toggle_music_listen(self) -> None:
        if self._music_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._music_player.stop()
            return
        item = self._music_list.currentItem() or self._music_list.item(0)
        if item is None:
            return
        if self._music_list.currentItem() is None:
            self._music_list.setCurrentItem(item)  # picks it too
        self._play_music(Path(item.data(Qt.ItemDataRole.UserRole)))

    def _on_music_playback_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._listen_btn.setText("■  Stop" if playing else "▶  Listen")

    # --- Export ------------------------------------------------------------

    def _resolve_sermon_duration_s(self) -> float:
        if self._project.sermon_duration_s > 0:
            return self._project.sermon_duration_s
        preview_dur = self._preview.duration_seconds()
        if preview_dur > 0:
            return preview_dur
        sermon = self._project.sermon_path
        if sermon and sermon.is_file():
            return ffprobe_duration_seconds(sermon.resolve())
        return 0.0

    def _export_progress(self, ratio: float, message: str) -> None:
        log_progress("export", message, ratio=ratio if ratio >= 0 else None)
        self._show_progress(ratio, message)
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    def _show_progress(self, ratio: float, message: str) -> None:
        if ratio >= 0:
            self._progress.setRange(0, 1000)
            self._progress.setValue(int(min(1000, max(0, ratio * 1000))))
            self._status.setText(f"{message}  ·  {int(ratio * 100)}%")
        else:
            # Unknown progress: an indeterminate (busy) bar.
            self._progress.setRange(0, 0)
            self._status.setText(message)

    def _generate_resolve(self) -> None:
        if self._busy:
            return
        self._project.project_name = self._project_name_edit.text().strip() or default_export_project_name()
        self._project.auto_captions = self._captions_enabled()
        self._project.caption_style = self._selected_caption_style().name
        self._project.reel_layout = self._reel_layout_check.isChecked()
        log_step("export", f"Generate Resolve project requested for {self._project.project_name!r}")
        if self._project.segments:
            seg_summary = ", ".join(
                f"{s.start_text}–{s.end_text}" for s in self._project.segments[:4]
            )
            log_info("export", f"Segments: {seg_summary}")
        if self._project.clip_paths:
            log_info("export", f"Clips: {[str(p) for p in self._project.clip_paths]}")
        elif self._project.joined_clip_path:
            log_info("export", f"Joined reel: {self._project.joined_clip_path}")
        if self._project.sermon_path:
            log_info("export", f"Sermon source: {self._project.sermon_path}")

        self._busy = True
        self._update_nav()
        self._export_progress(-1.0, "Preparing Resolve export…")

        sermon = self._project.sermon_path
        if sermon and sermon.is_file():
            if self._project.sermon_duration_s <= 0:
                self._export_progress(-1.0, "Reading sermon length…")
                try:
                    self._project.sermon_duration_s = self._resolve_sermon_duration_s()
                except Exception as exc:  # noqa: BLE001
                    self._busy = False
                    self._update_nav()
                    self._on_resolve_export_failed(f"Could not read sermon duration: {exc}")
                    return
                if self._project.sermon_duration_s <= 0:
                    self._busy = False
                    self._update_nav()
                    self._on_resolve_export_failed(
                        "The service video's length isn't known yet. Wait for it to finish "
                        "loading, then try again."
                    )
                    return

        try:
            path = generate_resolve_project(self._project, progress_cb=self._export_progress)
        except Exception as exc:  # noqa: BLE001
            self._busy = False
            self._update_nav()
            self._on_resolve_export_failed(str(exc))
        else:
            self._busy = False
            self._on_resolve_exported(path)
            self._update_nav()

    def _on_resolve_exported(self, path: Path) -> None:
        log_step("export", f"Export finished: {path}")
        self._last_export_path = path
        self._export_result_card.set_title("Project ready")
        self._export_result.setText(f"Saved to {path.parent}\nOpening it in DaVinci Resolve…")
        self._export_result_card.setVisible(True)
        # Start after _generate_resolve clears the busy flag (_run_job ignores busy calls).
        QTimer.singleShot(0, lambda: self._open_in_resolve(path))

    def _open_in_resolve(self, path: Path) -> None:
        project_name = self._project.project_name or path.stem
        log_step("export", f"Opening {path.name} in DaVinci Resolve")

        def job(*, progress_cb, should_cancel):
            return open_resolve_project(
                path,
                project_name=project_name,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
            )

        def on_ok(name: str) -> None:
            log_step("export", f"Resolve project opened: {name}")
            self._export_result_card.set_title(f"Opened in DaVinci Resolve as “{name}”")
            self._export_result.setText(
                f"Saved to {path.parent}\nIn Resolve, go to Deliver to render the MP4."
            )

        def on_fail(message: str) -> None:
            log_error("export", f"Resolve open failed: {message}")
            self._export_result.setText(
                f"Saved to {path.parent}\nCouldn't open it in Resolve automatically: {message}\n"
                f"In Resolve choose File → Import → Timeline… and pick {path.name}."
            )
            reveal_in_finder(path)

        self._run_job(job, on_ok=on_ok, on_fail=on_fail, message="Opening in DaVinci Resolve…")

    def _on_resolve_export_failed(self, message: str) -> None:
        log_error("export", message)
        QMessageBox.critical(self, "Wordly", message)

    # --- Job runner --------------------------------------------------------

    def _run_job(self, fn, *, on_ok, on_fail=None, message: str = "Working…") -> None:
        if self._busy:
            return
        self._wait_for_job_thread()
        self._busy = True
        log_step("wizard", "Background job started")
        # Unparented QThread — parenting to the window caused aborts when a long
        # export overlapped thread teardown from a prior job.
        thread = QThread()
        worker = _JobWorker(fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run, Qt.ConnectionType.QueuedConnection)
        worker.progress.connect(self._on_job_progress, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_job_failed, Qt.ConnectionType.QueuedConnection)
        worker.finished_ok.connect(self._on_job_finished_ok, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(thread.quit, Qt.ConnectionType.QueuedConnection)
        worker.finished_ok.connect(thread.quit, Qt.ConnectionType.QueuedConnection)
        thread.finished.connect(self._clear_job_thread)
        self._thread = thread
        self._worker = worker
        self._job_on_ok = on_ok
        self._job_on_fail = on_fail
        self._show_progress(-1.0, message)
        self._update_nav()
        thread.start()

    def _wait_for_job_thread(self) -> None:
        thread, worker = self._thread, self._worker
        self._thread = None
        self._worker = None
        if thread is None:
            return
        if thread.isRunning():
            thread.quit()
            thread.wait(30_000)
        # Its finished() may still be queued; keep it alive until that is handled.
        self._retired_jobs.append((thread, worker))

    @Slot(str)
    def _on_job_failed(self, msg: str) -> None:
        self._finish_job(False, msg, None)

    @Slot(object)
    def _on_job_finished_ok(self, result: object) -> None:
        self._finish_job(True, "", result)

    @Slot()
    def _clear_job_thread(self) -> None:
        sender = self.sender()
        if isinstance(sender, QThread):
            # finished() is emitted from inside the thread just before it stops.
            sender.wait(5_000)
        if sender is self._thread:
            self._retired_jobs.append((self._thread, self._worker))
            self._thread = None
            self._worker = None
        # Release only threads that have completely stopped.
        self._retired_jobs = [(t, w) for t, w in self._retired_jobs if not t.isFinished()]

    def _on_job_progress(self, ratio: float, message: str) -> None:
        log_progress("wizard", message, ratio=ratio if ratio >= 0 else None)
        self._show_progress(ratio, message)

    def _finish_job(self, ok: bool, err: str, result) -> None:
        on_ok = self._job_on_ok
        on_fail = self._job_on_fail
        self._job_on_ok = None
        self._job_on_fail = None
        self._busy = False
        self._progress.setRange(0, 1000)
        self._progress.setValue(1000 if ok else 0)
        self._update_nav()
        if ok:
            if on_ok and result is not None:
                on_ok(result)
        else:
            if on_fail:
                on_fail(err)
            elif err and err != "Cancelled":
                QMessageBox.warning(self, "Wordly", err)

    def _cancel_job(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._status.setText("Cancelling…")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._wsl_repaint_hardening:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
        self._save_prefs()
        # Cancel first: waiting on a running job before cancelling it froze the
        # window for up to 30 s on close.
        if self._worker is not None:
            self._worker.cancel()
        self._wait_for_job_thread()
        for thread, _ in self._retired_jobs:
            thread.wait(5_000)
        super().closeEvent(event)
