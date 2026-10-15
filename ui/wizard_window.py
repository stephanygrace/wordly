from __future__ import annotations

import os
import platform
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCursor, QMoveEvent, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QInputDialog,
    QVBoxLayout,
    QWidget,
)

from models.project import ClipSegment, MusicChoice, ProjectState, VerseChoice
from services.downloader import download_backend_description, download_facebook_video
from services.multi_clip import export_clips, merge_clips
from services.music_downloader import AUDIO_EXTENSIONS, download_instrumental
from services.trimmer import ffprobe_duration_seconds
from services.resolve_export import generate_resolve_project
from services.resolve_launcher import install_import_script, open_resolve_project, reveal_in_finder
from services.resolve_template import template_available
from services.claude_client import MissingApiKey, api_key, save_api_key
from services.highlights import HighlightPlan, find_highlights, format_clock
from services.highlights import format_timecode as highlight_timecode
from services.caption_style import DEFAULT_STYLE_NAME, CaptionStyle, load_caption_styles
from services.transcribe import Caption, transcribe_clips, whisper_backend_label, whisper_model_name
from ui.preview_player import PreviewPlayer
from ui.timecode_edit import TimecodeLineEdit
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
    validate_segment_times,
)


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


class WizardWindow(QMainWindow):
    """Production wizard for sermon highlight reels."""

    _STEP_TITLES = (
        "Download",
        "Timestamps",
        "Preview",
        "Captions",
        "Verse & music",
        "Project",
    )

    # Background ffprobe signals (emitted from a daemon thread, consumed on UI thread)
    _sermon_probed = Signal(float)
    _sermon_probe_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        ensure_directories()
        self.setWindowTitle("Wordly — Sermon Highlight Studio")
        self.resize(880, 580)
        self.setMinimumSize(720, 480)
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
        self._wsl_repaint_hardening = _running_on_wsl()

        self._step_ticks: list[QFrame] = []
        self._step_indicator = self._build_step_indicator()

        self._stack = QStackedWidget()
        self._stack.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._stack.setAutoFillBackground(True)
        self._steps = [
            self._wrap_step(self._build_download_step()),
            self._wrap_step(self._build_timestamps_step()),
            self._wrap_step(self._build_preview_step()),
            self._wrap_step(self._build_captions_step()),
            self._wrap_step(self._build_verse_music_step()),
            self._wrap_step(self._build_project_export_step()),
        ]
        for widget in self._steps:
            self._stack.addWidget(widget)
        self._stack.currentChanged.connect(self._on_step_changed)

        nav = QHBoxLayout()
        nav.setSpacing(6)
        nav.setContentsMargins(0, 0, 0, 0)
        self._back_btn = QPushButton("← Back")
        self._back_btn.setObjectName("NavBackButton")
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn = QPushButton("Next →")
        self._next_btn.setObjectName("NavNextButton")
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn = QPushButton("Cancel job")
        self._cancel_btn.setObjectName("CancelJobButton")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.setVisible(False)
        self._back_btn.clicked.connect(self._go_back)
        self._next_btn.clicked.connect(self._go_next)
        self._cancel_btn.clicked.connect(self._cancel_job)
        nav.addWidget(self._back_btn)
        nav.addWidget(self._next_btn)
        nav.addStretch(1)
        nav.addWidget(self._cancel_btn)

        self._progress = QProgressBar()
        self._progress.setRange(0, 1000)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("Ready")
        self._progress.setVisible(False)
        self._status = QLabel("Ready")
        self._status.setObjectName("JobStatusLabel")
        self._status.setWordWrap(True)
        self._status.setVisible(False)

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
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12, 6, 12, 8)
        layout.setSpacing(6)
        layout.addWidget(self._step_indicator)
        layout.addWidget(self._stack, stretch=1)
        layout.addWidget(self._progress)
        layout.addWidget(self._status)
        layout.addLayout(nav)
        outer.addWidget(body, stretch=1)

        self.setCentralWidget(root)
        self._configure_opaque_surfaces(root)
        self._configure_opaque_surfaces(body)

        self._apply_styles()
        self._apply_pointer_cursors()
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
        self._update_nav()
        self._refresh_timestamp_hints()
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

    def _build_step_indicator(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("StepIndicatorBar")
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        self._step_title_label = QLabel(self._STEP_TITLES[0])
        self._step_title_label.setObjectName("StepCurrentTitle")
        self._step_title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        tick_row = QHBoxLayout()
        tick_row.setSpacing(6)
        for title in self._STEP_TITLES:
            tick = QFrame()
            tick.setObjectName("StepTick")
            tick.setFixedHeight(4)
            tick.setToolTip(title)
            self._step_ticks.append(tick)
            tick_row.addWidget(tick, stretch=1)

        outer.addLayout(tick_row)
        outer.addWidget(self._step_title_label)
        return bar

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
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
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

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #16181c;
                color: #eceff4;
                font-size: 12px;
            }
            QLabel {
                background-color: transparent;
            }
            QWidget#WslTitleBar {
                background-color: #1e2228;
                border-bottom: 1px solid #2f343c;
            }
            QLabel#WslTitleLabel {
                color: #eceff4;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#WslTitleButton {
                min-width: 0;
                padding: 0;
                border-radius: 6px;
                background-color: transparent;
                border: 1px solid transparent;
                color: #c4c7cc;
                font-size: 12px;
            }
            QPushButton#WslTitleButton:hover {
                background-color: #2a3038;
                border-color: #3c424c;
            }
            QPushButton#WslTitleCloseButton:hover {
                background-color: #c94c4c;
                border-color: #e57373;
                color: #ffffff;
            }
            QLabel#StepCurrentTitle {
                color: #b8bcc4;
                font-size: 10px;
                font-weight: 600;
                letter-spacing: 0.8px;
            }
            QFrame#StepTick {
                background-color: #2f343c;
                border: none;
                border-radius: 2px;
                min-height: 4px;
                max-height: 4px;
            }
            QFrame#StepTickDone {
                background-color: #3d8b6e;
            }
            QFrame#StepTickActive {
                background-color: #3d5afe;
                min-height: 5px;
                max-height: 5px;
            }
            QGroupBox {
                border: 1px solid #2f343c;
                border-radius: 8px;
                margin-top: 8px;
                padding: 10px 10px 8px 10px;
                background-color: #1e2228;
                font-weight: 600;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: #9aa0a6;
                background-color: transparent;
            }
            QLabel#StepTitle {
                font-size: 16px;
                font-weight: 700;
                color: #f3f4f6;
                padding: 0 0 4px 0;
                margin-bottom: 0;
            }
            QLabel#StepSubtitle, QLabel#MutedHelpLabel {
                color: #9aa0a6;
                font-size: 11px;
                padding: 0 0 8px 0;
                margin-bottom: 4px;
            }
            QLabel#DurationHint, QLabel#InlineCaption {
                color: #9aa0a6;
                font-size: 11px;
                padding: 0 0 6px 0;
                margin-bottom: 4px;
            }
            QLabel#InlineCaption {
                padding: 0 2px 0 0;
                margin: 0;
            }
            QLabel#FieldError {
                color: #f28b82;
                font-size: 10px;
                padding: 2px 0 6px 0;
                margin-top: 0;
            }
            QLabel#JobStatusLabel {
                color: #c4c7cc;
                font-size: 11px;
                padding: 2px 0;
            }
            QLineEdit, QPlainTextEdit, QComboBox {
                border: 1px solid #3c424c;
                border-radius: 6px;
                padding: 5px 8px;
                background-color: #0f1114;
                selection-background-color: #3d5afe;
            }
            QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {
                border-color: #5b6cff;
            }
            QLineEdit#InvalidField {
                border-color: #e57373;
                background-color: #1a1214;
            }
            QPushButton {
                background-color: #2a3038;
                border: 1px solid #3c424c;
                border-radius: 6px;
                padding: 5px 10px;
                color: #eceff4;
            }
            QPushButton#NavBackButton, QPushButton#NavNextButton, QPushButton#CancelJobButton {
                min-height: 28px;
                max-height: 28px;
                font-size: 12px;
                font-weight: 500;
                padding: 4px 12px;
                border-radius: 6px;
            }
            QPushButton#NavBackButton {
                background-color: #2a3038;
                border: 1px solid #3c424c;
                color: #c4c7cc;
            }
            QPushButton#NavBackButton:hover:enabled {
                background-color: #323842;
                border-color: #5b6cff;
                color: #ffffff;
            }
            QPushButton#NavBackButton:disabled {
                background-color: #22262c;
                border-color: #2f343c;
                color: #5c6370;
            }
            QPushButton#NavNextButton:enabled {
                background-color: #243328;
                border: 1px solid #3d8b6e;
                color: #d4f0e4;
            }
            QPushButton#NavNextButton:hover:enabled {
                background-color: #2f4f42;
                border-color: #4caf88;
                color: #ffffff;
            }
            QPushButton#NavNextButton:disabled {
                background-color: #22262c;
                border-color: #3c424c;
                color: #5c6370;
            }
            QPushButton#CancelJobButton:enabled {
                background-color: rgba(42, 34, 36, 0.55);
                border: 1px solid #c85a5a;
                color: #f0b4b4;
            }
            QPushButton#CancelJobButton:hover:enabled {
                background-color: rgba(58, 42, 44, 0.85);
                border-color: #e57373;
                color: #ffd6d6;
            }
            QPushButton#CancelJobButton:disabled {
                background-color: transparent;
                border-color: #3c424c;
                color: #5c6370;
            }
            QWidget#SegmentComposer {
                background-color: #1a1d22;
                border: 1px solid #2f343c;
                border-radius: 10px;
            }
            QLabel#SegmentComposerTitle {
                color: #9aa0a6;
                font-size: 11px;
                font-weight: 600;
                letter-spacing: 0.4px;
                padding: 0;
                margin: 0;
            }
            QPushButton#SegmentAddButton:enabled {
                background-color: #243328;
                border: 1px solid #3d8b6e;
                color: #d4f0e4;
                padding: 4px 10px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#SegmentAddButton:hover:enabled {
                background-color: #2f4f42;
                border-color: #4caf88;
                color: #ffffff;
            }
            QPushButton#SegmentRemoveButton:enabled {
                background-color: transparent;
                border: 1px solid #4a5058;
                color: #b8bcc4;
                padding: 4px 10px;
                font-size: 11px;
            }
            QPushButton#SegmentRemoveButton:hover:enabled {
                border-color: #c85a5a;
                color: #f0b4b4;
            }
            QPushButton#SegmentRemoveButton:disabled {
                color: #5c6370;
                border-color: #2f343c;
            }
            QPushButton#GhostButton, QPushButton#DurationQuickButton {
                background-color: transparent;
                border: 1px solid #4a5058;
                color: #b8bcc4;
                padding: 4px 8px;
                font-size: 11px;
                border-radius: 6px;
            }
            QPushButton#GhostButton:hover:enabled,
            QPushButton#DurationQuickButton:hover:enabled {
                border-color: #6b7078;
                color: #eceff4;
            }
            QPushButton#DurationQuickButton {
                min-width: 36px;
                padding: 4px 6px;
            }
            QPushButton:hover {
                border-color: #5c6370;
                background-color: #323842;
            }
            QPushButton:pressed {
                background-color: #252a32;
            }
            QPushButton:disabled {
                color: #6b7078;
                background-color: #22262c;
                border-color: #2f343c;
            }
            QPushButton#AccentButton {
                background-color: #3d5afe;
                border-color: #5b6cff;
                font-weight: 600;
                color: #ffffff;
            }
            QPushButton#AccentButton:hover {
                background-color: #4f6bff;
            }
            QListWidget {
                border: 1px solid #2f343c;
                border-radius: 8px;
                background-color: #0f1114;
                padding: 4px;
            }
            QListWidget::item {
                padding: 6px 8px;
                border-radius: 4px;
            }
            QListWidget::item:selected {
                background-color: #2a3558;
                color: #e8eaed;
            }
            QListWidget::item:hover {
                background-color: #222832;
            }
            QProgressBar {
                min-height: 12px;
                border: 1px solid #2f343c;
                border-radius: 6px;
                background-color: #0f1114;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #3d5afe;
                border-radius: 6px;
            }
            QScrollArea {
                border: none;
                background-color: #16181c;
            }
            QScrollArea > QWidget > QWidget {
                background-color: #16181c;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid #5c6370;
                background-color: #0f1114;
            }
            QCheckBox::indicator:checked {
                background-color: #3d5afe;
                border-color: #5b6cff;
            }
            """
        )

    def _apply_pointer_cursors(self) -> None:
        root = self.centralWidget()
        if root is None:
            return
        for btn in root.findChildren(QPushButton):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)

    def _refresh_step_indicator(self) -> None:
        current = self._stack.currentIndex()
        titles = self._STEP_TITLES
        self._step_title_label.setText(titles[current].upper())
        for i, tick in enumerate(self._step_ticks):
            if i < current:
                tick.setObjectName("StepTickDone")
            elif i == current:
                tick.setObjectName("StepTickActive")
            else:
                tick.setObjectName("StepTick")
            tick.style().unpolish(tick)
            tick.style().polish(tick)

    def _configure_step_layout(self, layout: QVBoxLayout) -> None:
        layout.setContentsMargins(2, 2, 4, 4)
        layout.setSpacing(8)

    @staticmethod
    def _configure_form_layout(form: QFormLayout) -> None:
        form.setSpacing(8)
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(12)
        form.setContentsMargins(4, 8, 4, 8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

    # --- Step builders -----------------------------------------------------

    def _build_download_step(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self._configure_step_layout(layout)

        title = QLabel("Paste Facebook or YouTube URL")
        title.setObjectName("StepTitle")
        layout.addWidget(title)

        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("https://www.facebook.com/... or https://youtu.be/...")

        self._download_backend_note = QLabel(
            f"Download backend: {download_backend_description()}."
        )
        self._download_backend_note.setObjectName("MutedHelpLabel")
        self._download_backend_note.setWordWrap(True)

        self._open_local_btn = QPushButton("Open local sermon instead…")
        self._open_local_btn.clicked.connect(self._open_local_sermon)
        self._download_btn = QPushButton("Download sermon")
        self._download_btn.setObjectName("AccentButton")
        self._download_btn.clicked.connect(self._start_download)

        form_box = QGroupBox("Source")
        form = QFormLayout(form_box)
        self._configure_form_layout(form)
        form.addRow("Facebook URL", self._url_edit)

        layout.addWidget(form_box)
        layout.addSpacing(6)
        layout.addWidget(self._download_backend_note)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._download_btn)
        btn_row.addWidget(self._open_local_btn)
        layout.addLayout(btn_row)
        self._download_result = QLabel("No sermon loaded yet.")
        self._download_result.setObjectName("MutedHelpLabel")
        self._download_result.setWordWrap(True)
        layout.addWidget(self._download_result)
        return w

    def _build_timestamps_step(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self._configure_step_layout(layout)

        title = QLabel("Highlight timestamps")
        title.setObjectName("StepTitle")
        subtitle = QLabel("Add one or more start/end ranges. Wordly trims and joins them in order.")
        subtitle.setObjectName("StepSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self._seg_duration_hint = QLabel("Sermon duration: — (load a sermon on step 1)")
        self._seg_duration_hint.setObjectName("DurationHint")
        layout.addWidget(self._seg_duration_hint)
        layout.addSpacing(4)

        self._find_highlights_btn = QPushButton("Find highlights with Claude")
        self._find_highlights_btn.setObjectName("AccentButton")
        self._find_highlights_btn.setToolTip(
            "Transcribes the service, finds the sermon and lets Claude pick 2–3 highlights "
            "(max 2 minutes) plus the Bible verse. You can still edit everything."
        )
        self._find_highlights_btn.clicked.connect(self._find_highlights)
        highlights_row = QHBoxLayout()
        highlights_row.addWidget(self._find_highlights_btn)
        highlights_row.addStretch(1)
        layout.addLayout(highlights_row)
        self._highlights_summary = QLabel("")
        self._highlights_summary.setObjectName("MutedHelpLabel")
        self._highlights_summary.setWordWrap(True)
        self._highlights_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._highlights_summary)

        segments_box = QGroupBox("Segments")
        segments_box.setObjectName("SegmentListBox")
        segments_layout = QVBoxLayout(segments_box)
        segments_layout.setContentsMargins(8, 10, 8, 8)
        segments_layout.setSpacing(6)
        self._segment_list = QListWidget()
        self._segment_list.setObjectName("SegmentList")
        self._segment_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._segment_list.setMinimumHeight(88)
        segments_layout.addWidget(self._segment_list)
        layout.addWidget(segments_box, stretch=1)

        self._seg_start = TimecodeLineEdit()
        self._seg_start.setPlaceholderText("e.g. 1:25:00")
        self._seg_end = TimecodeLineEdit()
        self._seg_end.setPlaceholderText("e.g. 1:25:30")
        self._seg_end_30_btn = QPushButton("+30s")
        self._seg_end_30_btn.setObjectName("DurationQuickButton")
        self._seg_end_30_btn.setToolTip("Set end to start + 30 seconds")
        self._seg_end_30_btn.clicked.connect(lambda: self._set_segment_end_offset(30))
        self._seg_end_60_btn = QPushButton("+60s")
        self._seg_end_60_btn.setObjectName("DurationQuickButton")
        self._seg_end_60_btn.setToolTip("Set end to start + 60 seconds")
        self._seg_end_60_btn.clicked.connect(lambda: self._set_segment_end_offset(60))
        self._seg_label = QLineEdit()
        self._seg_label.setPlaceholderText("Label (optional)")

        self._seg_start_error = QLabel("")
        self._seg_start_error.setObjectName("FieldError")
        self._seg_end_error = QLabel("")
        self._seg_end_error.setObjectName("FieldError")
        self._seg_range_error = QLabel("")
        self._seg_range_error.setObjectName("FieldError")

        self._seg_start.textChanged.connect(self._on_timestamp_fields_changed)
        self._seg_end.textChanged.connect(self._on_timestamp_fields_changed)

        fields_row = QHBoxLayout()
        fields_row.setSpacing(8)
        start_cap = QLabel("Start")
        start_cap.setObjectName("InlineCaption")
        fields_row.addWidget(start_cap)
        fields_row.addWidget(self._seg_start, 2)
        fields_row.addWidget(self._seg_end_30_btn)
        fields_row.addWidget(self._seg_end_60_btn)
        end_cap = QLabel("End")
        end_cap.setObjectName("InlineCaption")
        fields_row.addWidget(end_cap)
        fields_row.addWidget(self._seg_end, 2)
        label_cap = QLabel("Label")
        label_cap.setObjectName("InlineCaption")
        fields_row.addWidget(label_cap)
        fields_row.addWidget(self._seg_label, 3)

        errors_row = QHBoxLayout()
        errors_row.setSpacing(8)
        errors_row.addWidget(self._seg_start_error, stretch=1)
        errors_row.addWidget(self._seg_end_error, stretch=1)

        add_panel = QWidget()
        add_panel.setObjectName("SegmentComposer")
        add_layout = QVBoxLayout(add_panel)
        add_layout.setContentsMargins(10, 10, 10, 10)
        add_layout.setSpacing(8)

        composer_title = QLabel("New segment")
        composer_title.setObjectName("SegmentComposerTitle")
        add_layout.addWidget(composer_title)
        add_layout.addLayout(fields_row)
        add_layout.addLayout(errors_row)
        add_layout.addWidget(self._seg_range_error)

        self._add_segment_btn = QPushButton("Add segment")
        self._add_segment_btn.setObjectName("SegmentAddButton")
        self._add_segment_btn.clicked.connect(self._add_segment)
        remove_btn = QPushButton("Remove")
        remove_btn.setObjectName("SegmentRemoveButton")
        remove_btn.clicked.connect(self._remove_segment)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)
        actions_row.addWidget(self._add_segment_btn)
        actions_row.addWidget(remove_btn)
        self._export_trimmed_btn = QPushButton("Export trimmed clips…")
        self._export_trimmed_btn.setObjectName("AccentButton")
        self._export_trimmed_btn.clicked.connect(self._export_trimmed_clips_clicked)
        self._export_merged_btn = QPushButton("Export merged video…")
        self._export_merged_btn.setObjectName("AccentButton")
        self._export_merged_btn.clicked.connect(self._export_merged_video_clicked)
        actions_row.addSpacing(12)
        actions_row.addWidget(self._export_trimmed_btn)
        actions_row.addWidget(self._export_merged_btn)
        actions_row.addStretch(1)
        add_layout.addLayout(actions_row)
        layout.addWidget(add_panel)

        hint = QLabel("Type digits — colons are added automatically. +30s / +60s set end from start.")
        hint.setObjectName("MutedHelpLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return w

    def _build_preview_step(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self._configure_step_layout(layout)
        title = QLabel("Preview clip")
        title.setObjectName("StepTitle")
        layout.addWidget(title)
        self._preview = PreviewPlayer()
        self._preview.duration_changed_s.connect(self._on_preview_duration)
        self._preview_segment = QComboBox()
        self._preview_segment.currentIndexChanged.connect(self._sync_preview_segment)
        layout.addWidget(self._preview_segment)
        layout.addWidget(self._preview)
        return w

    def _build_captions_step(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self._configure_step_layout(layout)
        title = QLabel("Captions")
        title.setObjectName("StepTitle")
        subtitle = QLabel(
            "Transcribe the clips with Whisper, then fix any wrong words below. "
            "Edited captions go onto the Resolve timeline as styled titles."
        )
        subtitle.setObjectName("StepSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        backend = whisper_backend_label()
        self._auto_captions_check = QCheckBox("Add captions to the Resolve timeline")
        self._auto_captions_check.setChecked(
            backend is not None and settings().value(KEY_AUTO_CAPTIONS, False, type=bool)
        )
        self._auto_captions_check.setEnabled(backend is not None)
        self._auto_captions_check.toggled.connect(self._on_captions_toggled)
        layout.addWidget(self._auto_captions_check)

        try:
            self._caption_styles = load_caption_styles()
        except (ValueError, TypeError) as exc:
            log_warn("captions", f"caption_styles.json ignored: {exc}")
            self._caption_styles = {}
        if not self._caption_styles:
            self._caption_styles = {DEFAULT_STYLE_NAME: CaptionStyle(DEFAULT_STYLE_NAME)}
        self._caption_style_combo = QComboBox()
        self._caption_style_combo.addItems(list(self._caption_styles))
        saved_style = settings().value(KEY_CAPTION_STYLE, DEFAULT_STYLE_NAME, type=str)
        if saved_style in self._caption_styles:
            self._caption_style_combo.setCurrentText(saved_style)
        self._caption_style_combo.currentTextChanged.connect(self._on_caption_style_changed)
        self._caption_preview = QLabel()
        self._caption_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._caption_preview.setMinimumHeight(44)
        self._caption_preview.setStyleSheet(
            "background: #2b3a45; border-radius: 6px; padding: 6px; font-weight: 700; font-size: 18px;"
        )
        self._caption_preview_index = 0
        self._caption_preview_timer = QTimer(self)
        self._caption_preview_timer.setInterval(450)
        self._caption_preview_timer.timeout.connect(self._tick_caption_preview)
        style_box = QGroupBox("Style")
        style_form = QFormLayout(style_box)
        self._configure_form_layout(style_form)
        style_form.addRow("Look", self._caption_style_combo)
        style_form.addRow("Preview", self._caption_preview)
        layout.addWidget(style_box)
        self._render_caption_preview()

        self._transcribe_btn = QPushButton("Transcribe clips")
        self._transcribe_btn.setObjectName("AccentButton")
        self._transcribe_btn.clicked.connect(lambda: self._start_transcription())
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._transcribe_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._captions_table = QTableWidget(0, 3)
        self._captions_table.setHorizontalHeaderLabels(["Clip", "Time", "Caption"])
        self._captions_table.verticalHeader().setVisible(False)
        header = self._captions_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._captions_table.setMinimumHeight(160)
        self._captions_table.itemChanged.connect(self._on_caption_edited)
        layout.addWidget(self._captions_table, stretch=1)

        self._captions_status = QLabel("")
        self._captions_status.setObjectName("MutedHelpLabel")
        self._captions_status.setWordWrap(True)
        layout.addWidget(self._captions_status)
        return w

    def _selected_caption_style(self) -> CaptionStyle:
        name = self._caption_style_combo.currentText()
        return self._caption_styles.get(name) or next(iter(self._caption_styles.values()))

    def _on_caption_style_changed(self, name: str) -> None:
        settings().setValue(KEY_CAPTION_STYLE, name)
        self._project.caption_style = name
        self._caption_preview_index = 0
        self._render_caption_preview()
        self._refresh_export_step_note()

    def _tick_caption_preview(self) -> None:
        self._caption_preview_index += 1
        self._render_caption_preview()

    def _render_caption_preview(self) -> None:
        """Approximate the caption look; the highlight cycles like it will in Resolve."""
        style = self._selected_caption_style()
        sample = "God is good all the time"
        if self._project.captions_current():
            first = next((c for clip in self._project.clip_captions for c in clip), None)
            if first is not None:
                sample = first.text
        words = style.display_text(sample).split()
        lit = self._caption_preview_index % max(1, len(words)) if style.highlight_color else -1
        html = " ".join(
            f'<span style="color:{style.highlight_color if i == lit else style.text_color}">{w}</span>'
            for i, w in enumerate(words)
        )
        self._caption_preview.setText(html)
        if style.highlight_color and self._stack.currentIndex() == 3:
            self._caption_preview_timer.start()
        else:
            self._caption_preview_timer.stop()

    def _captions_enabled(self) -> bool:
        return hasattr(self, "_auto_captions_check") and self._auto_captions_check.isChecked()

    def _on_captions_toggled(self, on: bool) -> None:
        settings().setValue(KEY_AUTO_CAPTIONS, bool(on))
        self._refresh_captions_step()
        self._refresh_export_step_note()
        self._update_nav()

    def _refresh_captions_step(self) -> None:
        if not hasattr(self, "_captions_table"):
            return
        enabled = self._captions_enabled()
        backend = whisper_backend_label()
        self._transcribe_btn.setEnabled(enabled and not self._busy)
        self._captions_table.setEnabled(enabled)

        if not self._project.captions_current():
            self._fill_captions_table([])
        else:
            self._fill_captions_table(self._project.clip_captions)

        if backend is None:
            text = "Install mlx-whisper or faster-whisper to enable captions."
        elif not enabled:
            text = "Captions are off. Next skips this step."
        elif self._project.captions_current():
            count = sum(len(c) for c in self._project.clip_captions)
            text = (
                f"{count} captions from {len(self._project.clip_captions)} clip(s). "
                "Double-click a caption to edit it."
            )
        elif self._project.clip_captions:
            text = "Clips changed since the last transcription. Transcribe again (or click Next)."
        else:
            text = (
                f"Not transcribed yet. Uses {backend} ({whisper_model_name()} model); "
                "the first run downloads the model. Next transcribes automatically."
            )
        self._captions_status.setText(text)
        self._render_caption_preview()

    def _fill_captions_table(self, clip_captions: list[list[Caption]]) -> None:
        table = self._captions_table
        table.blockSignals(True)
        table.setRowCount(0)
        for clip_idx, captions in enumerate(clip_captions):
            for cap_idx, cap in enumerate(captions):
                row = table.rowCount()
                table.insertRow(row)
                clip_item = QTableWidgetItem(f"Clip{clip_idx + 1:03d}")
                time_item = QTableWidgetItem(f"{cap.start:5.1f}s – {cap.end:5.1f}s")
                text_item = QTableWidgetItem(cap.text)
                for item in (clip_item, time_item):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                text_item.setData(Qt.ItemDataRole.UserRole, (clip_idx, cap_idx))
                table.setItem(row, 0, clip_item)
                table.setItem(row, 1, time_item)
                table.setItem(row, 2, text_item)
        table.blockSignals(False)

    def _on_caption_edited(self, item: QTableWidgetItem) -> None:
        key = item.data(Qt.ItemDataRole.UserRole)
        if item.column() != 2 or not key:
            return
        clip_idx, cap_idx = key
        captions = self._project.clip_captions[clip_idx]
        old = captions[cap_idx]
        text = item.text().strip()
        if text == old.text:
            return
        captions[cap_idx] = Caption(old.start, old.end, text, old.words)
        self._refresh_export_step_note()

    def _start_transcription(self, *, advance_to: int | None = None) -> None:
        clips = [p for p in self._project.clip_paths if p.exists()]
        if not clips:
            QMessageBox.warning(self, "Wordly", "Trim the clips in the Preview step first.")
            return
        self._status.setText("Transcribing clips…")
        self._progress.setVisible(True)
        self._progress.setFormat("Transcribing — starting…")
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
            self._status.setText(f"{count} captions ready")
            self._refresh_captions_step()
            self._refresh_export_step_note()
            self._update_nav()
            if advance_to is not None:
                self._stack.setCurrentIndex(advance_to)

        def on_fail(message: str) -> None:
            self._refresh_captions_step()
            if message and message != "Cancelled":
                QMessageBox.warning(self, "Wordly", f"Transcription failed:\n{message}")

        self._run_job(job, on_ok=on_ok, on_fail=on_fail)

    def _build_verse_step(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self._configure_step_layout(layout)
        title = QLabel("Bible verse")
        title.setObjectName("StepTitle")
        subtitle = QLabel(
            "Type or paste the reference on the first line and the verse text below. "
            "It is written into verse.srt / verse.txt for your Resolve export."
        )
        subtitle.setObjectName("StepSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self._verse_edit = QPlainTextEdit()
        self._verse_edit.setPlaceholderText(
            "e.g.\nJohn 3:16\nFor God so loved the world…"
        )
        self._verse_edit.setMinimumHeight(120)
        self._verse_edit.textChanged.connect(self._sync_manual_verse)
        layout.addWidget(self._verse_edit)
        layout.addStretch(1)
        return w

    def _build_music_step(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self._configure_step_layout(layout)
        title = QLabel("Instrumental bed")
        title.setObjectName("StepTitle")
        subtitle = QLabel(
            "Paste a YouTube (or other) audio URL to download, or choose an audio file "
            "already on your computer."
        )
        subtitle.setObjectName("StepSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self._music_url_edit = QLineEdit()
        self._music_url_edit.setPlaceholderText("https://www.youtube.com/watch?v=…")
        self._download_music_url_btn = QPushButton("Download from URL")
        self._download_music_url_btn.setObjectName("AccentButton")
        self._download_music_url_btn.clicked.connect(self._download_music_from_url)
        self._local_music_btn = QPushButton("Choose local audio…")
        self._local_music_btn.clicked.connect(self._browse_local_music)

        form_box = QGroupBox("Source")
        form = QFormLayout(form_box)
        self._configure_form_layout(form)
        form.addRow("Audio URL", self._music_url_edit)

        layout.addWidget(form_box)
        layout.addSpacing(6)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._download_music_url_btn)
        btn_row.addWidget(self._local_music_btn)
        layout.addLayout(btn_row)
        self._music_status = QLabel("No instrumental loaded yet.")
        self._music_status.setObjectName("MutedHelpLabel")
        self._music_status.setWordWrap(True)
        layout.addWidget(self._music_status)
        layout.addStretch(1)
        return w

    def _build_verse_music_step(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self._configure_step_layout(layout)

        title = QLabel("Bible verse and instrumental")
        title.setObjectName("StepTitle")
        subtitle = QLabel(
            "Add the verse and choose the instrumental bed for the Resolve export."
        )
        subtitle.setObjectName("StepSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        verse_box = QGroupBox("Bible verse")
        verse_layout = QVBoxLayout(verse_box)
        verse_layout.setContentsMargins(8, 10, 8, 8)
        verse_layout.setSpacing(6)
        verse_hint = QLabel(
            "Type or paste the reference on the first line and the verse text below."
        )
        verse_hint.setObjectName("MutedHelpLabel")
        verse_hint.setWordWrap(True)
        self._verse_edit = QPlainTextEdit()
        self._verse_edit.setPlaceholderText(
            "e.g.\nJohn 3:16\nFor God so loved the world…"
        )
        self._verse_edit.setMinimumHeight(105)
        self._verse_edit.textChanged.connect(self._sync_manual_verse)
        verse_layout.addWidget(verse_hint)
        verse_layout.addWidget(self._verse_edit)
        layout.addWidget(verse_box)

        music_box = QGroupBox("Instrumental bed")
        music_layout = QVBoxLayout(music_box)
        music_layout.setContentsMargins(8, 10, 8, 8)
        music_layout.setSpacing(6)
        music_hint = QLabel(
            "Paste a YouTube (or other) audio URL to download, or choose an audio file "
            "already on your computer."
        )
        music_hint.setObjectName("MutedHelpLabel")
        music_hint.setWordWrap(True)
        self._music_url_edit = QLineEdit()
        self._music_url_edit.setPlaceholderText("https://www.youtube.com/watch?v=…")
        self._download_music_url_btn = QPushButton("Download from URL")
        self._download_music_url_btn.setObjectName("AccentButton")
        self._download_music_url_btn.clicked.connect(self._download_music_from_url)
        self._local_music_btn = QPushButton("Choose local audio…")
        self._local_music_btn.clicked.connect(self._browse_local_music)

        form = QFormLayout()
        self._configure_form_layout(form)
        form.addRow("Audio URL", self._music_url_edit)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._download_music_url_btn)
        btn_row.addWidget(self._local_music_btn)
        btn_row.addStretch(1)
        self._music_status = QLabel("No instrumental loaded yet.")
        self._music_status.setObjectName("MutedHelpLabel")
        self._music_status.setWordWrap(True)
        music_layout.addWidget(music_hint)
        music_layout.addLayout(form)
        music_layout.addLayout(btn_row)
        music_layout.addWidget(self._music_status)
        layout.addWidget(music_box)
        layout.addStretch(1)
        return w

    def _build_name_step(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self._configure_step_layout(layout)
        title = QLabel("Project name")
        title.setObjectName("StepTitle")
        subtitle = QLabel(
            f"Defaults to last Sunday's date ({default_export_project_name()}). "
            "Used for exports/{name}/{name}.fcpxml."
        )
        subtitle.setObjectName("StepSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        self._project_name_edit = QLineEdit(default_export_project_name())
        self._last_suggested_project_name = self._project_name_edit.text()
        self._project_name_edit.textChanged.connect(self._on_project_name_changed)
        form_box = QGroupBox("Export name")
        form = QFormLayout(form_box)
        self._configure_form_layout(form)
        form.addRow("Name", self._project_name_edit)
        layout.addWidget(form_box)
        return w

    def _build_export_step(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self._configure_step_layout(layout)
        title = QLabel("Export")
        title.setObjectName("StepTitle")
        layout.addWidget(title)
        template_note = QLabel("")
        self._export_template_note = template_note
        self._refresh_export_step_note()
        template_note.setObjectName("MutedHelpLabel")
        template_note.setWordWrap(True)
        host_note = QLabel(
            "Primary target: macOS. Keep the .fcpxml beside the media/ folder."
        )
        host_note.setObjectName("MutedHelpLabel")
        host_note.setWordWrap(True)
        layout.addWidget(template_note)
        layout.addWidget(host_note)
        self._export_result = QLabel("")
        self._export_result.setObjectName("MutedHelpLabel")
        self._export_result.setWordWrap(True)
        layout.addWidget(self._export_result)
        layout.addStretch(1)
        return w

    def _build_project_export_step(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self._configure_step_layout(layout)

        title = QLabel("Layers and project name")
        title.setObjectName("StepTitle")
        subtitle = QLabel(
            "Confirm the Resolve project name and export details before generating."
        )
        subtitle.setObjectName("StepSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self._project_name_edit = QLineEdit(default_export_project_name())
        self._last_suggested_project_name = self._project_name_edit.text()
        self._project_name_edit.textChanged.connect(self._on_project_name_changed)
        name_box = QGroupBox("Project name")
        name_form = QFormLayout(name_box)
        self._configure_form_layout(name_form)
        name_form.addRow("Name", self._project_name_edit)
        layout.addWidget(name_box)

        layers_box = QGroupBox("Resolve timeline")
        layers_layout = QVBoxLayout(layers_box)
        layers_layout.setContentsMargins(8, 10, 8, 8)
        layers_layout.setSpacing(6)
        template_note = QLabel("")
        self._export_template_note = template_note
        self._refresh_export_step_note()
        template_note.setObjectName("MutedHelpLabel")
        template_note.setWordWrap(True)
        host_note = QLabel(
            "Resolve Free exports without watermarks when you avoid Studio-only effects. "
            "Keep the .fcpxml beside the media/ folder."
        )
        host_note.setObjectName("MutedHelpLabel")
        host_note.setWordWrap(True)
        layers_layout.addWidget(template_note)
        layers_layout.addWidget(host_note)

        self._reel_layout_check = QCheckBox("Reel layout — blurred background, video centred, verse above")
        self._reel_layout_check.setChecked(settings().value(KEY_REEL_LAYOUT, True, type=bool))
        self._reel_layout_check.toggled.connect(lambda on: settings().setValue(KEY_REEL_LAYOUT, bool(on)))
        layers_layout.addWidget(self._reel_layout_check)
        self._export_captions_note = QLabel("")
        self._export_captions_note.setObjectName("MutedHelpLabel")
        self._export_captions_note.setWordWrap(True)
        layers_layout.addWidget(self._export_captions_note)
        layout.addWidget(layers_box)

        self._export_result = QLabel("")
        self._export_result.setObjectName("MutedHelpLabel")
        self._export_result.setWordWrap(True)
        layout.addWidget(self._export_result)
        layout.addStretch(1)
        return w

    # --- Navigation --------------------------------------------------------

    def _step_complete(self, idx: int) -> bool:
        if idx == 0:
            path = self._project.sermon_path
            return bool(path and path.exists())
        if idx == 1:
            return bool(self._project.segments)
        if idx == 2:
            return self._step_complete(0) and self._step_complete(1)
        if idx == 3:
            # Next transcribes first when captions are on but not generated yet.
            return True
        if idx == 4:
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
        if idx == 5:
            return bool(self._project_name_edit.text().strip())
        return True

    def _update_nav(self) -> None:
        idx = self._stack.currentIndex()
        titles = self._STEP_TITLES
        self._back_btn.setEnabled(idx > 0)
        can_advance = not self._busy and self._step_complete(idx)
        self._next_btn.setEnabled(can_advance)
        if self._busy and idx == 2:
            self._next_btn.setText("Trimming…")
        elif self._busy and idx == 3:
            self._next_btn.setText("Transcribing…")
        elif idx == len(titles) - 1:
            self._next_btn.setText("Generate Resolve project")
        else:
            self._next_btn.setText("Next →")
        self._progress.setVisible(self._busy)
        self._status.setVisible(self._busy)
        if hasattr(self, "_download_btn"):
            self._download_btn.setEnabled(not self._busy)
        if hasattr(self, "_open_local_btn"):
            self._open_local_btn.setEnabled(not self._busy)
        if hasattr(self, "_find_highlights_btn"):
            self._find_highlights_btn.setEnabled(not self._busy)
        if hasattr(self, "_transcribe_btn"):
            self._transcribe_btn.setEnabled(self._captions_enabled() and not self._busy)
        self._refresh_step_indicator()
        self._refresh_add_segment_enabled()

    def _on_step_changed(self, index: int) -> None:
        if self._last_step_index == 2 and index != 2 and hasattr(self, "_preview"):
            self._preview.stop()
        self._last_step_index = index
        if index == 1:
            self._refresh_timestamp_hints()
        elif index == 2:
            self._load_preview_for_current_sermon()
        elif index == 3:
            self._refresh_captions_step()
        if hasattr(self, "_caption_preview_timer"):
            self._render_caption_preview()
        elif index == 5:
            self._refresh_default_project_name()
            self._refresh_export_step_note()
        self._stack.update()
        self._request_full_repaint()
        self._update_nav()

    def _on_project_name_changed(self) -> None:
        self._update_nav()
        self._refresh_export_step_note()

    def _refresh_default_project_name(self) -> None:
        suggested = default_export_project_name()
        current = self._project_name_edit.text().strip()
        if not current or current == self._last_suggested_project_name:
            self._project_name_edit.setText(suggested)
        self._last_suggested_project_name = suggested
        self._refresh_export_step_note()

    def _refresh_export_step_note(self) -> None:
        if hasattr(self, "_export_captions_note"):
            if not self._captions_enabled():
                text = "Captions: off (turn them on in the Captions step)."
            elif self._project.captions_current():
                count = sum(len(c) for c in self._project.clip_captions)
                text = (
                    f"Captions: {count} reviewed captions in “{self._selected_caption_style().name}” style "
                    "will be added above the video, plus captions.srt."
                )
            else:
                text = "Captions: on — clips will be transcribed during export."
            self._export_captions_note.setText(text)
        if not hasattr(self, "_export_template_note"):
            return
        name = self._project_name_edit.text().strip() or default_export_project_name()
        if template_available():
            self._export_template_note.setText(
                "Wordly writes a Resolve Free import bundle: FCPXML timeline, media/ "
                "(Clip001…, instrumental), verse.srt, and OPEN_IN_RESOLVE.txt. "
                f"Output: exports/{name}/{name}.fcpxml"
            )
        else:
            self._export_template_note.setText(
                "Add assets/resolve_templates/sermon-highlights.fcpxml "
                "(or set WORDLY_RESOLVE_TEMPLATE). See docs/TEMPLATE_AUTHORING.md."
            )

    def _go_back(self) -> None:
        if self._stack.currentIndex() == 2 and hasattr(self, "_preview"):
            self._preview.stop()
        if self._busy:
            self._cancel_job()
        if self._stack.currentIndex() > 0:
            self._stack.setCurrentIndex(self._stack.currentIndex() - 1)

    def _go_next(self) -> None:
        idx = self._stack.currentIndex()
        if idx == 2 and hasattr(self, "_preview"):
            self._preview.stop()
        if idx == 1 and not self._project.segments:
            QMessageBox.warning(self, "Wordly", "Add at least one timestamp segment.")
            return
        if idx == 2:
            self._advance_from_preview()
            return
        if idx == 3 and self._captions_enabled() and not self._project.captions_current():
            self._start_transcription(advance_to=4)
            return
        if idx >= len(self._steps) - 1:
            self._generate_resolve()
            return
        self._stack.setCurrentIndex(idx + 1)

    # --- Actions -----------------------------------------------------------

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
        self._project.sermon_path = resolved
        self._project.sermon_duration_s = 0.0
        self._download_result.setText(f"Loading: {resolved.name}…")
        self._status.setText(f"Loading — {resolved}")
        # Start the preview immediately; the player can buffer while ffprobe runs.
        self._load_preview_for_current_sermon()
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
            dur_str = format_timecode(duration)
            self._download_result.setText(f"Loaded: {path.name} ({dur_str})")
            if hasattr(self, "_preview"):
                self._preview.set_duration_seconds(duration)
        self._status.setText(f"Sermon ready — {path}")
        self._refresh_timestamp_hints()
        self._update_nav()

    @Slot(str)
    def _on_sermon_probe_failed(self, msg: str) -> None:
        QMessageBox.warning(self, "Wordly", msg)

    def _refresh_timestamp_hints(self) -> None:
        if not hasattr(self, "_seg_duration_hint"):
            return
        if self._project.sermon_duration_s > 0:
            self._seg_duration_hint.setText(
                f"Sermon duration: {format_timecode(self._project.sermon_duration_s)}"
            )
        else:
            self._seg_duration_hint.setText("Sermon duration: — (load a sermon on step 1)")
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

    def _set_field_valid(self, field: QLineEdit, error_label: QLabel, message: str | None) -> None:
        invalid = bool(message)
        field.setObjectName("InvalidField" if invalid else "")
        field.style().unpolish(field)
        field.style().polish(field)
        error_label.setText(message or "")
        error_label.setVisible(invalid)

    def _on_timestamp_fields_changed(self) -> None:
        start_err = self._field_time_error(self._seg_start.text())
        end_err = self._field_time_error(self._seg_end.text())
        self._set_field_valid(self._seg_start, self._seg_start_error, start_err)
        self._set_field_valid(self._seg_end, self._seg_end_error, end_err)

        range_err: str | None = None
        if not start_err and not end_err:
            start_t = self._seg_start.text().strip()
            end_t = self._seg_end.text().strip()
            if start_t and end_t:
                try:
                    media = self._project.sermon_duration_s or None
                    validate_segment_times(start_t, end_t, media_duration_s=media)
                except ValueError as exc:
                    range_err = str(exc)
        self._seg_range_error.setText(range_err or "")
        self._seg_range_error.setVisible(bool(range_err))
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
        if hasattr(self, "_add_segment_btn"):
            self._add_segment_btn.setEnabled(self._segment_form_valid() and not self._busy)

    def _start_download(self) -> None:
        url = self._url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "Wordly", "Paste a Facebook or YouTube URL first.")
            return
        self._save_prefs()
        self._project.fb_url = url

        def job(*, progress_cb, should_cancel):
            return download_facebook_video(
                url,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
            )

        self._run_job(job, on_ok=lambda path: self._set_sermon(path))

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

    def _find_highlights(self) -> None:
        sermon = self._project.sermon_path
        if not sermon or not sermon.is_file():
            QMessageBox.warning(self, "Wordly", "Load the service video first (step 1).")
            return
        if self._project.segments:
            answer = QMessageBox.question(
                self,
                "Wordly",
                "Replace your current segments with Claude's highlights?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not self._ensure_api_key():
            return
        self._highlights_summary.setText("Working… transcribing a whole service takes a few minutes the first time.")

        def job(*, progress_cb, should_cancel):
            return find_highlights(sermon, progress_cb=progress_cb, should_cancel=should_cancel)

        def on_fail(message: str) -> None:
            self._highlights_summary.setText("")
            if message and message != "Cancelled":
                QMessageBox.warning(self, "Wordly", f"Couldn't find highlights:\n{message}")

        self._run_job(job, on_ok=self._apply_highlights, on_fail=on_fail)

    def _apply_highlights(self, plan: HighlightPlan) -> None:
        self._project.segments = [
            ClipSegment(highlight_timecode(h.start), highlight_timecode(h.end), h.title) for h in plan.highlights
        ]
        self._project.clip_paths = []
        self._project.joined_clip_path = None
        self._segment_list.clear()
        self._preview_segment.clear()
        for seg in self._project.segments:
            self._segment_list.addItem(seg.display_name)
            self._preview_segment.addItem(seg.display_name)
        if plan.verse_reference and not self._verse_edit.toPlainText().strip():
            # Pre-fill the verse step; the wording is from Claude, so it should be checked.
            self._verse_edit.setPlainText(f"{plan.verse_reference}\n{plan.verse_text}")
        lines = [
            f"Sermon found at {format_clock(plan.sermon_start)}–{format_clock(plan.sermon_end)}"
            + (f" — “{plan.sermon_title}”" if plan.sermon_title else ""),
        ]
        for i, h in enumerate(plan.highlights, start=1):
            lines.append(f"{i}. {h.title} ({format_clock(h.start)}, {h.duration:.0f}s): {h.reason}")
        lines.append(f"Total {plan.total_s:.0f}s.")
        if plan.verse_reference:
            lines.append(f"Suggested verse: {plan.verse_reference} — {plan.verse_reason} (check the wording)")
        lines.extend(plan.notes)
        self._highlights_summary.setText("\n".join(lines))
        self._on_timestamp_fields_changed()
        self._update_nav()

    def _set_segment_end_offset(self, offset_s: float) -> None:
        start_t = normalize_four_digit_timecode(self._seg_start.text().strip())
        if not start_t:
            QMessageBox.warning(self, "Wordly", "Enter a start time first.")
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
            QMessageBox.warning(self, "Wordly", str(exc))
            return
        self._seg_end.setText(end_t)
        self._on_timestamp_fields_changed()

    def _add_segment(self) -> None:
        start_t = self._seg_start.text().strip()
        end_t = self._seg_end.text().strip()
        if not start_t or not end_t:
            QMessageBox.warning(self, "Wordly", "Enter both start and end timecodes.")
            return
        try:
            media = self._project.sermon_duration_s or None
            validate_segment_times(start_t, end_t, media_duration_s=media)
        except ValueError as exc:
            QMessageBox.warning(self, "Wordly", str(exc))
            return
        seg = ClipSegment(start_t, end_t, self._seg_label.text().strip())
        self._project.segments.append(seg)
        self._project.clip_paths = []
        self._project.joined_clip_path = None
        self._segment_list.addItem(seg.display_name)
        self._preview_segment.addItem(seg.display_name)
        self._seg_start.clear()
        self._seg_end.clear()
        self._seg_label.clear()
        self._on_timestamp_fields_changed()
        self._update_nav()

    def _remove_segment(self) -> None:
        row = self._segment_list.currentRow()
        if row < 0:
            return
        self._segment_list.takeItem(row)
        self._preview_segment.removeItem(row)
        del self._project.segments[row]
        self._project.clip_paths = []
        self._project.joined_clip_path = None
        self._update_nav()

    def _load_preview_for_current_sermon(self) -> None:
        if not hasattr(self, "_preview"):
            return
        path = self._project.sermon_path
        if not path:
            self._preview.clear()
            return
        self._preview.load_file(path)
        self._sync_preview_segment()

    def _sync_preview_segment(self) -> None:
        row = self._preview_segment.currentIndex()
        if row < 0 or row >= len(self._project.segments):
            return
        seg = self._project.segments[row]
        try:
            from utils.timecode import parse_timecode

            start_ms = int(parse_timecode(seg.start_text).total_seconds * 1000)
            end_ms = int(parse_timecode(seg.end_text).total_seconds * 1000)
            self._preview.set_trim_window_ms(start_ms, end_ms)
            self._preview.seek_trim_start()
        except Exception:
            pass

    def _clips_up_to_date(self) -> bool:
        valid = [p for p in self._project.clip_paths if p.exists()]
        return bool(valid) and len(valid) == len(self._project.segments)

    def _advance_from_preview(self) -> None:
        if not self._project.sermon_path:
            QMessageBox.warning(self, "Wordly", "Load a sermon first.")
            return
        if not self._project.segments:
            QMessageBox.warning(self, "Wordly", "Add timestamp segments first.")
            return
        if self._clips_up_to_date():
            self._stack.setCurrentIndex(3)
            return
        self._start_trim_clips(advance_to=3)

    def _start_trim_clips(self, *, advance_to: int | None = None) -> None:
        if not self._project.sermon_path:
            QMessageBox.warning(self, "Wordly", "Load a sermon first.")
            return
        if not self._project.segments:
            QMessageBox.warning(self, "Wordly", "Add timestamp segments first.")
            return

        self._status.setText("Trimming clips…")
        self._progress.setVisible(True)
        self._progress.setFormat("Trimming — starting…")
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
            self._status.setText(f"{len(paths)} clip{'s' if len(paths) != 1 else ''} ready")
            self._update_nav()
            if advance_to is not None:
                self._stack.setCurrentIndex(advance_to)

        self._run_job(job, on_ok=on_ok, on_fail=lambda _err: None)

    def _export_trimmed_clips_clicked(self) -> None:
        if not self._project.sermon_path:
            QMessageBox.warning(self, "Wordly", "Load a sermon first.")
            return
        if not self._project.segments:
            QMessageBox.warning(self, "Wordly", "Add timestamp segments first.")
            return

        dest = QFileDialog.getExistingDirectory(
            self,
            "Choose folder for trimmed clips",
            str(DOWNLOADS),
        )
        if not dest:
            return
        output_dir = Path(dest).resolve()

        self._status.setText("Exporting trimmed clips…")
        self._progress.setVisible(True)
        self._progress.setFormat("Exporting — starting…")
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
            self._status.setText(f"Exported {len(paths)} clip(s) to {output_dir}")
            QMessageBox.information(
                self,
                "Wordly",
                f"Saved {len(paths)} clip(s) to:\n{output_dir}",
            )

        self._run_job(job, on_ok=on_ok)

    def _export_merged_video_clicked(self) -> None:
        if not self._project.sermon_path:
            QMessageBox.warning(self, "Wordly", "Load a sermon first.")
            return
        if not self._project.segments:
            QMessageBox.warning(self, "Wordly", "Add timestamp segments first.")
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

        self._status.setText("Exporting merged video…")
        self._progress.setVisible(True)
        self._progress.setFormat("Exporting — starting…")

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
            self._status.setText(f"Merged video ready — {merged}")
            QMessageBox.information(self, "Wordly", f"Saved merged video:\n{merged}")

        self._run_job(job, on_ok=on_ok)

    @classmethod
    def _parse_verse_input(cls, raw: str) -> tuple[str, str] | None:
        lines = [line.strip() for line in raw.strip().splitlines() if line.strip()]
        if len(lines) < 2:
            return None
        reference = lines[0]
        body = "\n".join(lines[1:]).strip()
        if not reference or not body:
            return None
        return reference, body

    def _sync_manual_verse(self) -> None:
        parsed = self._parse_verse_input(self._verse_edit.toPlainText())
        if parsed:
            self._project.selected_verse = VerseChoice(*parsed)
        else:
            self._project.selected_verse = None
        self._update_nav()

    def _download_music_from_url(self) -> None:
        url = self._music_url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "Wordly", "Paste a YouTube or direct audio URL first.")
            return
        self._run_instrumental_download(url, title=url[:80])

    def _run_instrumental_download(self, query: str, *, title: str) -> None:
        def job(*, progress_cb, should_cancel):
            return download_instrumental(query, progress_cb=progress_cb, should_cancel=should_cancel)

        def on_ok(path: Path) -> None:
            music = MusicChoice(title=title or path.stem, local_path=path)
            self._project.selected_music = music
            self._music_status.setText(f"Loaded: {path.name}")
            self._update_nav()

        self._run_job(job, on_ok=on_ok)

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
        self._project.selected_music = MusicChoice(title=p.stem, local_path=p)
        self._music_status.setText(f"Loaded: {p.name}")
        self._update_nav()

    def _resolve_sermon_duration_s(self) -> float:
        if self._project.sermon_duration_s > 0:
            return self._project.sermon_duration_s
        if hasattr(self, "_preview"):
            preview_dur = self._preview.duration_seconds()
            if preview_dur > 0:
                return preview_dur
        sermon = self._project.sermon_path
        if sermon and sermon.is_file():
            return ffprobe_duration_seconds(sermon.resolve())
        return 0.0

    def _export_progress(self, ratio: float, message: str) -> None:
        log_progress("export", message, ratio=ratio if ratio >= 0 else None)
        if ratio >= 0:
            self._progress.setValue(int(min(1000, max(0, ratio * 1000))))
            self._progress.setFormat(f"{int(ratio * 100)}% — {message[:80]}")
        else:
            self._progress.setFormat(message[:120])
        self._status.setText(message)
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

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
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._progress.setFormat("Preparing export…")
        self._status.setVisible(True)
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
                        "Sermon duration is not ready yet. Wait for the download step to "
                        "finish loading, then try again."
                    )
                    return

        try:
            path = generate_resolve_project(self._project, progress_cb=self._export_progress)
        except Exception as exc:  # noqa: BLE001
            self._on_resolve_export_failed(str(exc))
        else:
            self._on_resolve_exported(path)
        finally:
            self._busy = False
            self._progress.setValue(1000)
            self._progress.setFormat("Done")
            self._update_nav()

    def _on_resolve_exported(self, path: Path) -> None:
        log_step("export", f"Export finished: {path}")
        bundle_dir = path.parent
        self._export_result.setText(
            f"Saved Resolve import bundle:\n{path}\n\nMedia folder:\n{bundle_dir / 'media'}"
        )
        self._status.setText(f"Resolve project ready — {path}")
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
            self._status.setText(f"Opened in DaVinci Resolve — project “{name}”")
            self._export_result.setText(
                f"Opened in DaVinci Resolve as project “{name}”.\n\nExport folder:\n{path.parent}"
            )

        def on_fail(message: str) -> None:
            log_error("export", f"Resolve open failed: {message}")
            reveal_in_finder(path)
            QMessageBox.warning(
                self,
                "Wordly",
                f"Project saved to:\n{path}\n\nCould not open it in Resolve automatically:\n{message}",
            )

        self._run_job(job, on_ok=on_ok, on_fail=on_fail)

    def _on_resolve_export_failed(self, message: str) -> None:
        log_error("export", message)
        QMessageBox.critical(self, "Wordly", message)

    # --- Job runner --------------------------------------------------------

    def _run_job(self, fn, *, on_ok, on_fail=None) -> None:
        if self._busy:
            return
        self._wait_for_job_thread()
        self._busy = True
        self._cancel_btn.setVisible(True)
        log_step("wizard", "Background job started")
        self._update_nav()
        self._progress.setValue(0)
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
        if ratio >= 0:
            self._progress.setValue(int(min(1000, max(0, ratio * 1000))))
            pct = int(ratio * 100)
            self._progress.setFormat(f"{pct}% — {message[:80]}")
        else:
            self._progress.setFormat(message[:120])
        self._status.setText(message)

    def _finish_job(self, ok: bool, err: str, result) -> None:
        on_ok = self._job_on_ok
        on_fail = self._job_on_fail
        self._job_on_ok = None
        self._job_on_fail = None
        self._busy = False
        self._cancel_btn.setVisible(False)
        if ok:
            self._progress.setValue(1000)
            self._progress.setFormat("Done")
        else:
            self._progress.setValue(0)
            self._progress.setFormat("Ready")
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
