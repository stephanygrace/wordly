from __future__ import annotations

import threading
from functools import partial
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
    QShowEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from services.downloader import download_facebook_video
from services.renderer import render_vertical_reel
from services.trimmer import (
    clamp_trim_to_duration,
    default_clip_output_path,
    export_trimmed_clip,
    ffprobe_duration_seconds,
    ffprobe_has_audio,
    parse_trim_times,
)
from ui.controls_panel import ControlsPanel
from ui.preview_player import PreviewPlayer
from utils.app_settings import (
    KEY_GEOMETRY,
    KEY_LAST_CLIP_DIR,
    KEY_LAST_FB_URL,
    KEY_LAST_OUTPUT_STEM,
    KEY_LAST_SERMON_DIR,
    KEY_LAST_SERMON_FILE,
    KEY_LAST_TRIM_END_TEXT,
    KEY_LAST_TRIM_START_TEXT,
    KEY_RECENT_SERMONS,
    KEY_SPLITTER,
    settings,
)
from utils.paths import CLIPS, DOWNLOADS, EXPORTS, ensure_directories
from utils.recent_sermons import add_recent_path, clear_recent_paths, existing_recent_files
from utils.timecode import format_timecode
from utils.tool_versions import cli_version_token


class _DownloadWorker(QObject):
    finished_ok = Signal(object)
    failed = Signal(str)
    progress = Signal(float, str)

    def __init__(self, url: str, cancel_event: threading.Event, cookies_file: Path | None) -> None:
        super().__init__()
        self._url = url
        self._cancel = cancel_event
        self._cookies = cookies_file

    def run(self) -> None:
        try:
            from yt_dlp.utils import DownloadCancelled
        except ImportError:  # pragma: no cover
            DownloadCancelled = None

        try:
            path = download_facebook_video(
                self._url,
                progress_cb=lambda ratio, msg: self.progress.emit(ratio, msg),
                should_cancel=self._cancel.is_set,
                cookies_file=self._cookies,
            )
        except Exception as exc:  # noqa: BLE001
            if DownloadCancelled is not None and isinstance(exc, DownloadCancelled):
                self.failed.emit("Cancelled")
                return
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(path)


class _ExportWorker(QObject):
    finished_ok = Signal(object)
    failed = Signal(str)
    progress = Signal(float, str)

    def __init__(
        self,
        render_kwargs: dict,
        layout_path: Path | None,
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self._render_kwargs = render_kwargs
        self._layout_path = layout_path
        self._cancel = cancel_event

    def run(self) -> None:
        from utils.layout_template import default_layout, load_layout

        layout = load_layout(self._layout_path) if self._layout_path else default_layout()
        try:
            out = render_vertical_reel(
                **self._render_kwargs,
                layout=layout,
                should_cancel=self._cancel.is_set,
                progress_cb=lambda r, m: self.progress.emit(r, m),
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(out)


class _ClipWorker(QObject):
    finished_ok = Signal(object)
    failed = Signal(str)
    progress = Signal(float, str)

    def __init__(self, clip_kwargs: dict, cancel_event: threading.Event) -> None:
        super().__init__()
        self._clip_kwargs = clip_kwargs
        self._cancel = cancel_event

    def run(self) -> None:
        try:
            out = export_trimmed_clip(
                **self._clip_kwargs,
                should_cancel=self._cancel.is_set,
                progress_cb=lambda r, m: self.progress.emit(r, m),
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(out)


class _WhisperWorker(QObject):
    finished_ok = Signal(object)
    failed = Signal(str)
    progress = Signal(float, str)

    def __init__(
        self,
        media: Path,
        model: str,
        language: str | None,
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self._media = media
        self._model = model
        self._language = language
        self._cancel = cancel_event

    def run(self) -> None:
        from services.whisper_srt import transcribe_media_to_srt

        try:
            out = transcribe_media_to_srt(
                self._media,
                model=self._model,
                language=self._language,
                should_cancel=self._cancel.is_set,
                progress_cb=lambda r, m: self.progress.emit(r, m),
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(out)


class _DropHost(QWidget):
    """Accepts video file drops anywhere on the main content area."""

    sermon_dropped = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
                self.sermon_dropped.emit(path)
                event.acceptProposedAction()
                return
        super().dropEvent(event)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        ensure_directories()
        self._qsettings = settings()
        self._restored_layout = False
        self.setWindowTitle("Wordly — Sermon highlight production")
        self.resize(1280, 820)
        self.setMinimumSize(960, 640)

        self._sermon_path: Path | None = None
        self._media_duration_s: float | None = None
        self._has_sermon_audio: bool = True
        self._download_thread: QThread | None = None
        self._export_thread: QThread | None = None
        self._clip_thread: QThread | None = None
        self._whisper_thread: QThread | None = None
        self._job_cancel_event = threading.Event()
        self._download_cancel_event = threading.Event()
        self._download_in_progress = False
        self._encode_in_progress = False

        self.controls = ControlsPanel()
        self.controls.setMinimumWidth(400)
        self.controls.bind_settings(self._qsettings)
        self._restore_trim_fields()
        last_stem = self._qsettings.value(KEY_LAST_OUTPUT_STEM, "")
        if last_stem:
            self.controls.output_name.setText(str(last_stem))
        self._restore_last_facebook_url()
        self.preview = PreviewPlayer()
        self._sync_trim_preview()

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("Idle")

        preview_frame = QFrame()
        preview_frame.setObjectName("PreviewFrame")
        self._preview_frame = preview_frame
        pv_layout = QVBoxLayout(preview_frame)
        pv_layout.setContentsMargins(12, 12, 12, 12)
        pv_layout.setSpacing(10)
        preview_title = QLabel("Video preview")
        preview_title.setObjectName("PanelSectionTitle")
        pv_layout.addWidget(preview_title)
        pv_layout.addWidget(self.preview, stretch=1)
        pv_layout.addWidget(self.progress)

        self.preview.attach_space_shortcut(preview_frame)

        scroll = QScrollArea()
        scroll.setObjectName("ControlsScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.controls)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(scroll)
        splitter.addWidget(preview_frame)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([440, 880])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        self._splitter = splitter

        drop_host = _DropHost()
        drop_layout = QHBoxLayout(drop_host)
        drop_layout.setContentsMargins(0, 0, 0, 0)
        drop_layout.addWidget(splitter)
        self.setCentralWidget(drop_host)
        drop_host.sermon_dropped.connect(self._load_sermon)

        self._apply_styles()

        self.controls.download_btn.clicked.connect(self._on_download)
        self.controls.url_edit.returnPressed.connect(self._on_download)
        self.controls.open_local_btn.clicked.connect(self._on_open_local)
        self.controls.export_btn.clicked.connect(self._on_export)
        self.controls.save_clip_btn.clicked.connect(self._on_save_clip)
        self.controls.cancel_job_btn.clicked.connect(self._on_cancel_job)

        self.controls.timings_changed.connect(self._sync_trim_preview)
        self.controls.set_end_to_file_end_requested.connect(self._on_set_end_to_file_end)

        self._cancel_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._cancel_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._cancel_shortcut.setEnabled(False)
        self._cancel_shortcut.activated.connect(self._on_cancel_job)

        self._export_shortcut = QShortcut(QKeySequence("Ctrl+E"), self)
        self._export_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._export_shortcut.activated.connect(self._on_export)

        self._setup_menus()
        self._setup_status_bar()
        self._check_external_tools()
        self._update_status_sermon()
        self._sync_activity_lock()

    def _restore_trim_fields(self) -> None:
        ls = self._qsettings.value(KEY_LAST_TRIM_START_TEXT, "")
        le = self._qsettings.value(KEY_LAST_TRIM_END_TEXT, "")
        if isinstance(ls, str) and ls.strip():
            self.controls.start_edit.blockSignals(True)
            self.controls.start_edit.setText(ls.strip())
            self.controls.start_edit.blockSignals(False)
        if isinstance(le, str) and le.strip():
            self.controls.end_edit.blockSignals(True)
            self.controls.end_edit.setText(le.strip())
            self.controls.end_edit.blockSignals(False)

    def _persist_trim_fields(self) -> None:
        self._qsettings.setValue(KEY_LAST_TRIM_START_TEXT, self.controls.start_text())
        self._qsettings.setValue(KEY_LAST_TRIM_END_TEXT, self.controls.end_text())

    def _persist_session_fields(self) -> None:
        """Trim, layout template, verse overlay text, and download prefs."""
        self._persist_trim_fields()
        self.controls.persist_template_and_verse()
        self.controls.persist_download_prefs()

    def _restore_last_facebook_url(self) -> None:
        raw = self._qsettings.value(KEY_LAST_FB_URL, "")
        text = str(raw).strip() if raw is not None else ""
        if not text:
            return
        self.controls.url_edit.setText(text[:4096])

    def _persist_facebook_url_field(self) -> None:
        url = self.controls.facebook_url().strip()[:4096]
        if url:
            self._qsettings.setValue(KEY_LAST_FB_URL, url)
        else:
            self._qsettings.remove(KEY_LAST_FB_URL)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._restored_layout:
            return
        self._restored_layout = True
        geo = self._qsettings.value(KEY_GEOMETRY)
        if geo is not None:
            self.restoreGeometry(geo)
        st = self._qsettings.value(KEY_SPLITTER)
        if st is not None:
            self._splitter.restoreState(st)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._qsettings.setValue(KEY_GEOMETRY, self.saveGeometry())
        self._qsettings.setValue(KEY_SPLITTER, self._splitter.saveState())
        self._persist_session_fields()
        self._persist_facebook_url_field()
        super().closeEvent(event)

    def _setup_menus(self) -> None:
        bar = QMenuBar(self)
        self.setMenuBar(bar)

        file_menu = bar.addMenu("&File")
        open_act = QAction("Open sermon…", self)
        open_act.setShortcut("Ctrl+O")
        open_act.triggered.connect(self._on_open_local)
        file_menu.addAction(open_act)
        self._reopen_last_sermon_act = QAction("Reopen last sermon", self)
        self._reopen_last_sermon_act.triggered.connect(self._on_reopen_last_sermon)
        file_menu.addAction(self._reopen_last_sermon_act)
        copy_path_act = QAction("Copy sermon path", self)
        copy_path_act.setShortcut("Ctrl+Shift+C")
        copy_path_act.triggered.connect(self._copy_sermon_path)
        file_menu.addAction(copy_path_act)
        self._recent_menu = file_menu.addMenu("Open &recent")
        self._refresh_recent_menu()
        clear_recent_act = QAction("Clear recent list", self)
        clear_recent_act.triggered.connect(self._clear_recent_sermons)
        file_menu.addAction(clear_recent_act)
        file_menu.addSeparator()
        for label, folder in (
            ("Open &exports folder", EXPORTS),
            ("Open &clips folder", CLIPS),
            ("Open &downloads folder", DOWNLOADS),
        ):
            act = QAction(label, self)
            act.triggered.connect(partial(self._open_folder_path, folder))
            file_menu.addAction(act)
        clean_dl_act = QAction("Remove incomplete &downloads…", self)
        clean_dl_act.setToolTip("Delete yt-dlp fragment files (*.part, etc.) in the downloads folder")
        clean_dl_act.triggered.connect(self._on_remove_incomplete_downloads)
        file_menu.addAction(clean_dl_act)
        file_menu.addSeparator()
        quit_act = QAction("&Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        view_menu = bar.addMenu("&View")
        reset_layout_act = QAction("Reset window && splitter layout", self)
        reset_layout_act.setToolTip("Forget saved window size and panel widths (this session resets to defaults)")
        reset_layout_act.triggered.connect(self._reset_window_layout)
        view_menu.addAction(reset_layout_act)

        tools_menu = bar.addMenu("&Tools")
        whisper_act = QAction("Transcribe sermon to SRT (&Whisper)…", self)
        whisper_act.setToolTip(
            "Requires the `whisper` CLI on PATH (pip install openai-whisper). "
            "Writes <sermon>_whisper.srt next to the sermon and loads it for burn-in."
        )
        whisper_act.triggered.connect(self._on_whisper_transcribe)
        tools_menu.addAction(whisper_act)

        help_menu = bar.addMenu("&Help")
        about_act = QAction("&About Wordly", self)
        about_act.triggered.connect(self._about_wordly)
        help_menu.addAction(about_act)
        self._refresh_reopen_last_sermon_action()

    def _open_folder_path(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _refresh_recent_menu(self) -> None:
        self._recent_menu.clear()
        paths = existing_recent_files(self._qsettings, KEY_RECENT_SERMONS)
        if not paths:
            ph = QAction("(no recent files)", self)
            ph.setEnabled(False)
            self._recent_menu.addAction(ph)
            return
        for p in paths:
            act = QAction(p.name, self)
            act.setToolTip(str(p))
            act.triggered.connect(partial(self._load_sermon, p))
            self._recent_menu.addAction(act)

    def _refresh_reopen_last_sermon_action(self) -> None:
        raw = self._qsettings.value(KEY_LAST_SERMON_FILE, "")
        p = Path(str(raw)) if raw else None
        ok = bool(p and p.is_file())
        self._reopen_last_sermon_act.setEnabled(ok)
        if ok and p is not None:
            self._reopen_last_sermon_act.setToolTip(f"Open the last sermon file:\n{p.resolve()}")
        else:
            self._reopen_last_sermon_act.setToolTip(
                "Opens the sermon you last had loaded, if that file still exists."
            )

    def _on_reopen_last_sermon(self) -> None:
        raw = self._qsettings.value(KEY_LAST_SERMON_FILE, "")
        if not raw:
            return
        p = Path(str(raw))
        if not p.is_file():
            QMessageBox.warning(
                self,
                "Sermon not found",
                f"The saved sermon path is no longer available:\n{p}",
            )
            self._refresh_reopen_last_sermon_action()
            return
        self._load_sermon(p)

    def _maybe_autofill_export_stem(self, sermon: Path) -> None:
        cur = self.controls.output_name.text().strip()
        if cur and cur != "wordly-export":
            return
        self.controls.output_name.setText(ControlsPanel.sanitize_stem_segment(sermon.stem))

    def _about_wordly(self) -> None:
        QMessageBox.about(
            self,
            "About Wordly",
            "<h3>Wordly</h3>"
            "<p>Desktop sermon highlight production — vertical 9:16 reels for social.</p>"
            "<p>Trim with precision, add verse and music, optional burned-in subtitles, then export with FFmpeg.</p>"
            "<p style='color:#9aa0a6'>Python · PySide6 · FFmpeg · yt-dlp</p>",
        )

    def _setup_status_bar(self) -> None:
        sb = self.statusBar()
        self._status_sermon = QLabel("No sermon loaded")
        self._status_sermon.setObjectName("StatusSermonLabel")
        sb.addPermanentWidget(self._status_sermon, stretch=1)

    def _update_status_sermon(self) -> None:
        from PySide6.QtGui import QFontMetrics

        p = self._sermon_path
        if not p:
            self._status_sermon.setText("No sermon loaded")
            self._status_sermon.setToolTip("")
            return
        full = str(p.resolve())
        self._status_sermon.setToolTip(full)
        fm = QFontMetrics(self._status_sermon.font())
        sb_w = self.statusBar().width()
        reserve = 100
        avail = max(140, sb_w - reserve) if sb_w > reserve else 260
        label = f"Sermon: {p.name}"
        self._status_sermon.setText(fm.elidedText(label, Qt.TextElideMode.ElideMiddle, avail))

    def _check_external_tools(self) -> None:
        import shutil

        ok_bits: list[str] = []
        bad_bits: list[str] = []

        ff = shutil.which("ffmpeg")
        fp = shutil.which("ffprobe")
        if ff:
            ff_v = cli_version_token("ffmpeg") or "?"
            ok_bits.append(f"ffmpeg {ff_v}")
        else:
            bad_bits.append("ffmpeg missing")
        if fp:
            fp_v = cli_version_token("ffprobe") or "?"
            ok_bits.append(f"ffprobe {fp_v}")
        else:
            bad_bits.append("ffprobe missing")

        try:
            import yt_dlp  # noqa: F401

            ver = getattr(yt_dlp, "__version__", "") or "?"
            ok_bits.append(f"yt-dlp {ver}")
        except ImportError:
            bad_bits.append("yt-dlp not installed")

        if bad_bits:
            msg = " · ".join(bad_bits)
            if ok_bits:
                msg += "  |  " + " · ".join(ok_bits)
            msg += " — install missing pieces for download/export."
            self.statusBar().showMessage(msg, 60000)
        else:
            self.statusBar().showMessage(" · ".join(ok_bits), 12000)

    def _clear_recent_sermons(self) -> None:
        clear_recent_paths(self._qsettings, KEY_RECENT_SERMONS)
        self._refresh_recent_menu()

    def _reset_window_layout(self) -> None:
        self._qsettings.remove(KEY_GEOMETRY)
        self._qsettings.remove(KEY_SPLITTER)
        self.resize(1280, 820)
        self._splitter.setSizes([440, 880])
        QMessageBox.information(
            self,
            "Layout reset",
            "Saved window size and splitter widths were cleared. Defaults are applied now and on the next launch. "
            "Other saved preferences (sermon paths, trim, verse, template, music, etc.) are unchanged.",
        )

    def _sermon_dialog_start_dir(self) -> str:
        raw = self._qsettings.value(KEY_LAST_SERMON_DIR, "")
        if raw and Path(str(raw)).is_dir():
            return str(raw)
        return str(Path.home())

    def _copy_sermon_path(self) -> None:
        if not self._sermon_path:
            self.statusBar().showMessage("No sermon loaded.", 4000)
            return
        QApplication.clipboard().setText(str(self._sermon_path.resolve()))
        self.statusBar().showMessage("Sermon path copied to clipboard.", 4000)

    def _on_set_end_to_file_end(self) -> None:
        if self._media_duration_s is None:
            return
        self.controls.end_edit.setText(format_timecode(self._media_duration_s))
        self.controls.timings_changed.emit()

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #1a1d21;
                color: #e8eaed;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #2f343c;
                border-radius: 10px;
                margin-top: 14px;
                padding: 14px 12px 12px 12px;
                background-color: #22262c;
                font-weight: 600;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: #9aa0a6;
            }
            QLineEdit, QProgressBar, QComboBox, QPlainTextEdit {
                border: 1px solid #3c424c;
                border-radius: 8px;
                padding: 6px 8px;
                background-color: #121418;
                selection-background-color: #3d5afe;
            }
            QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {
                border-color: #5b6cff;
            }
            QPushButton:focus {
                border-color: #7a85ff;
            }
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
            QPlainTextEdit {
                padding: 8px;
            }
            QLabel#DurationHint {
                color: #9aa0a6;
                font-size: 12px;
            }
            QLabel#VolumePct {
                color: #9aa0a6;
                font-size: 12px;
            }
            QPushButton {
                background-color: #2a3038;
                border: 1px solid #3c424c;
                border-radius: 8px;
                padding: 7px 14px;
                color: #e8eaed;
            }
            QPushButton:hover {
                border-color: #5c6370;
            }
            QPushButton:pressed {
                background-color: #323842;
            }
            QPushButton#AccentButton {
                background-color: #3d5afe;
                border-color: #5b6cff;
                font-weight: 600;
            }
            QPushButton#AccentButton:hover {
                background-color: #4f6bff;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #2f343c;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                width: 16px;
                margin: -6px 0;
                background: #3d5afe;
                border-radius: 8px;
            }
            QFrame#PreviewFrame {
                border: 1px solid #2f343c;
                border-radius: 12px;
                background-color: #22262c;
            }
            QLabel#PreviewStatus, QLabel#PreviewHint, QLabel#PreviewTimecode {
                color: #9aa0a6;
            }
            QLabel#PreviewTimecode {
                font-family: ui-monospace, monospace;
                font-size: 12px;
            }
            QLabel#StatusSermonLabel {
                color: #c4c7cc;
                padding: 2px 8px;
            }
            QStatusBar {
                background-color: #22262c;
                color: #9aa0a6;
                border-top: 1px solid #2f343c;
            }
            QScrollArea { border: none; }
            QScrollArea#ControlsScroll {
                background-color: transparent;
            }
            QPushButton#CancelJobButton:enabled {
                background-color: #2a2224;
                border-color: #c85a5a;
                color: #f0b4b4;
            }
            QPushButton#CancelJobButton:enabled:hover {
                background-color: #3a282c;
                border-color: #e07070;
            }
            QPushButton#CancelJobButton:disabled {
                background-color: #25272c;
                border-color: #3c424c;
                color: #6b7078;
            }
            QLabel#PanelSectionTitle {
                color: #9aa0a6;
                font-size: 12px;
                font-weight: 600;
                letter-spacing: 0.02em;
            }
            QLabel#MutedHelpLabel {
                color: #8b9199;
                font-size: 12px;
            }
            QCheckBox {
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid #5c6370;
                background-color: #121418;
            }
            QCheckBox::indicator:checked {
                background-color: #3d5afe;
                border-color: #5b6cff;
            }
            QProgressBar {
                min-height: 14px;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #3d5afe;
                border-radius: 6px;
            }
            QMenuBar {
                background-color: #22262c;
                border-bottom: 1px solid #2f343c;
                padding: 2px;
            }
            QMenuBar::item:selected {
                background-color: #2a3038;
            }
            """
        )
        self.controls.export_btn.setObjectName("AccentButton")
        self.controls.cancel_job_btn.setObjectName("CancelJobButton")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_status_sermon()

    def _sync_activity_lock(self) -> None:
        self._cancel_shortcut.setEnabled(self._download_in_progress or self._encode_in_progress)
        self.controls.apply_activity_lock(
            downloading=self._download_in_progress,
            encoding=self._encode_in_progress,
        )

    def _set_download_busy(self, busy: bool, message: str = "") -> None:
        self._download_in_progress = busy
        if message:
            self.progress.setFormat(message)
        self._sync_activity_lock()

    def _set_encode_busy(self, busy: bool, message: str = "") -> None:
        self._encode_in_progress = busy
        if message:
            self.progress.setFormat(message)
        self._sync_activity_lock()

    def _on_download(self) -> None:
        url = self.controls.facebook_url()
        if not url:
            QMessageBox.warning(self, "Missing URL", "Paste a Facebook sermon or live video URL.")
            return
        if self._download_thread and self._download_thread.isRunning():
            return
        if self._export_thread and self._export_thread.isRunning():
            return
        if self._clip_thread and self._clip_thread.isRunning():
            return
        if self._whisper_thread and self._whisper_thread.isRunning():
            QMessageBox.information(self, "Busy", "Wait for Whisper transcription to finish before downloading.")
            return

        self._persist_facebook_url_field()

        self._download_cancel_event.clear()
        self._set_download_busy(True, "Downloading…")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        self._download_thread = QThread()
        worker = _DownloadWorker(url, self._download_cancel_event, self.controls.ytdlp_cookies_file())
        worker.moveToThread(self._download_thread)
        self._download_thread.started.connect(worker.run)
        worker.finished_ok.connect(self._on_download_finished)
        worker.failed.connect(self._on_download_failed)
        worker.progress.connect(self._on_download_progress)
        worker.finished_ok.connect(self._download_thread.quit)
        worker.failed.connect(self._download_thread.quit)
        worker.finished_ok.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        self._download_thread.finished.connect(self._download_thread.deleteLater)
        self._download_thread.start()

    def _on_download_progress(self, value: float, _msg: str) -> None:
        if value < 0:
            self.progress.setRange(0, 0)
            self.progress.setFormat("Downloading…")
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(value * 100))

    def _on_download_finished(self, path: object) -> None:
        self._set_download_busy(False, "Ready")
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        if isinstance(path, Path):
            self._load_sermon(path)

    def _on_download_failed(self, message: str) -> None:
        self._set_download_busy(False, "Ready")
        self.progress.setValue(0)
        if message == "Cancelled":
            self.progress.setFormat("Ready")
            self.progress.setRange(0, 100)
            self.statusBar().showMessage("Download cancelled.", 4000)
            return
        QMessageBox.critical(self, "Download failed", message)

    def _on_open_local(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Open sermon video",
            self._sermon_dialog_start_dir(),
            "Video (*.mp4 *.mov *.mkv *.webm *.m4v);;All files (*)",
        )
        if path_str:
            self._load_sermon(Path(path_str))

    def _load_sermon(self, path: Path) -> None:
        self._sermon_path = path
        self._qsettings.setValue(KEY_LAST_SERMON_DIR, str(path.parent.resolve()))
        self._qsettings.setValue(KEY_LAST_SERMON_FILE, str(path.resolve()))
        self._maybe_autofill_export_stem(path)
        self._media_duration_s = None
        self._has_sermon_audio = True
        self.controls.set_media_duration_hint(None)
        self.controls.set_end_at_file_end_enabled(False)
        self.preview.load_file(path)

        try:
            dur = ffprobe_duration_seconds(path)
            self._media_duration_s = dur
            self.controls.set_media_duration_hint(dur)
        except Exception:
            self._media_duration_s = None

        try:
            self._has_sermon_audio = ffprobe_has_audio(path)
        except Exception:
            self._has_sermon_audio = False

        self._clamp_timing_to_duration_on_load()
        self._sync_trim_preview()
        add_recent_path(self._qsettings, KEY_RECENT_SERMONS, path)
        self._refresh_recent_menu()
        self._update_status_sermon()
        self.controls.set_end_at_file_end_enabled(self._media_duration_s is not None)
        self._persist_session_fields()
        self._refresh_reopen_last_sermon_action()
        self.statusBar().showMessage(f"Loaded {path.name}", 4000)

    def _clamp_timing_to_duration_on_load(self) -> None:
        if self._media_duration_s is None or not self._sermon_path:
            return
        try:
            spec = parse_trim_times(self.controls.start_text(), self.controls.end_text())
        except ValueError:
            return
        clamped = clamp_trim_to_duration(spec, self._media_duration_s)
        if abs(clamped.start_seconds - spec.start_seconds) > 0.01:
            self.controls.start_edit.blockSignals(True)
            self.controls.start_edit.setText(format_timecode(clamped.start_seconds))
            self.controls.start_edit.blockSignals(False)
        if abs(clamped.end_seconds - spec.end_seconds) > 0.01:
            self.controls.end_edit.blockSignals(True)
            self.controls.end_edit.setText(format_timecode(clamped.end_seconds))
            self.controls.end_edit.blockSignals(False)

    def _resolved_trim_spec(self):
        spec = parse_trim_times(self.controls.start_text(), self.controls.end_text())
        if self._media_duration_s is not None:
            spec = clamp_trim_to_duration(spec, self._media_duration_s)
        return spec

    def _on_export(self) -> None:
        if not self._sermon_path or not self._sermon_path.exists():
            QMessageBox.warning(self, "No sermon", "Download or open a sermon video first.")
            return
        music = self.controls.piano_file()
        if not music or not music.exists():
            QMessageBox.warning(self, "Music", "Choose a local MP3 (or WAV/M4A) for the piano bed.")
            return
        srt = self.controls.subtitle_srt_file()
        if srt is not None:
            if srt.suffix.lower() not in {".srt", ".vtt", ".ass", ".ssa"}:
                QMessageBox.warning(
                    self,
                    "Subtitles",
                    "Only SubRip (.srt), WebVTT (.vtt), or ASS/SSA (.ass / .ssa) files are supported for burn-in.",
                )
                return
            if not srt.is_file():
                QMessageBox.warning(self, "Subtitles", f"Subtitle file not found:\n{srt}")
                return
        try:
            spec = self._resolved_trim_spec()
        except ValueError as exc:
            QMessageBox.warning(self, "Timing", str(exc))
            return

        if self._export_thread and self._export_thread.isRunning():
            return
        if self._clip_thread and self._clip_thread.isRunning():
            QMessageBox.information(self, "Busy", "Wait for the clip save to finish before exporting.")
            return
        if self._whisper_thread and self._whisper_thread.isRunning():
            QMessageBox.information(self, "Busy", "Wait for Whisper transcription to finish before exporting.")
            return
        if self._download_thread and self._download_thread.isRunning():
            QMessageBox.information(self, "Busy", "Wait for the download to finish before exporting.")
            return

        out = EXPORTS / f"{self.controls.output_stem()}.mp4"
        if out.exists():
            ans = QMessageBox.question(
                self,
                "Overwrite file?",
                f"This file already exists:\n{out}\n\nReplace it with a new export?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        self._job_cancel_event = threading.Event()
        self._set_encode_busy(True, "Rendering…")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        render_kwargs = dict(
            sermon_path=self._sermon_path,
            piano_path=music,
            output_path=out,
            start_s=spec.start_seconds,
            end_s=spec.end_seconds,
            verse_reference=self.controls.verse_reference() or " ",
            verse_text=self.controls.verse_body() or " ",
            sermon_volume_pct=self.controls.sermon_volume(),
            piano_volume_pct=self.controls.piano_volume(),
            piano_fade_in=self.controls.piano_fade_in(),
            piano_fade_out=self.controls.piano_fade_out(),
            has_sermon_audio=self._has_sermon_audio,
            srt_path=srt,
        )
        layout_path = self.controls.selected_template_path()

        self._export_thread = QThread()
        worker = _ExportWorker(render_kwargs, layout_path, self._job_cancel_event)
        worker.moveToThread(self._export_thread)
        self._export_thread.started.connect(worker.run)
        worker.finished_ok.connect(self._on_export_finished)
        worker.failed.connect(self._on_export_failed)
        worker.progress.connect(self._on_encode_progress)
        worker.finished_ok.connect(self._export_thread.quit)
        worker.failed.connect(self._export_thread.quit)
        worker.finished_ok.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        self._export_thread.finished.connect(self._export_thread.deleteLater)
        self._export_thread.start()

    def _on_save_clip(self) -> None:
        if not self._sermon_path or not self._sermon_path.exists():
            QMessageBox.warning(self, "No sermon", "Download or open a sermon video first.")
            return
        try:
            spec = self._resolved_trim_spec()
        except ValueError as exc:
            QMessageBox.warning(self, "Timing", str(exc))
            return

        if self._download_thread and self._download_thread.isRunning():
            QMessageBox.information(self, "Busy", "Wait for the download to finish before saving a clip.")
            return
        if self._export_thread and self._export_thread.isRunning():
            QMessageBox.information(self, "Busy", "Wait for the current export to finish.")
            return
        if self._whisper_thread and self._whisper_thread.isRunning():
            QMessageBox.information(self, "Busy", "Wait for Whisper transcription to finish before saving a clip.")
            return
        if self._clip_thread and self._clip_thread.isRunning():
            return

        default_path = default_clip_output_path(self._sermon_path, spec.start_seconds, spec.end_seconds)
        clip_start = self._qsettings.value(KEY_LAST_CLIP_DIR, "")
        start_dir = str(default_path.parent) if default_path.parent.is_dir() else self._sermon_dialog_start_dir()
        if clip_start and Path(str(clip_start)).is_dir():
            start_dir = str(clip_start)
        out_str, _ = QFileDialog.getSaveFileName(
            self,
            "Save trimmed clip",
            str(Path(start_dir) / default_path.name),
            "MP4 video (*.mp4);;All files (*)",
        )
        if not out_str:
            return
        out_path = Path(out_str)
        if out_path.suffix.lower() != ".mp4":
            out_path = out_path.with_suffix(".mp4")
        if out_path.exists():
            ans = QMessageBox.question(
                self,
                "Overwrite file?",
                f"This file already exists:\n{out_path}\n\nReplace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        self._qsettings.setValue(KEY_LAST_CLIP_DIR, str(out_path.parent.resolve()))

        if self._clip_thread and self._clip_thread.isRunning():
            return

        self._job_cancel_event = threading.Event()
        self._set_encode_busy(True, "Saving clip…")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        clip_kwargs = dict(
            sermon_path=self._sermon_path,
            output_path=out_path,
            start_s=spec.start_seconds,
            end_s=spec.end_seconds,
            has_audio=self._has_sermon_audio,
        )

        self._clip_thread = QThread()
        worker = _ClipWorker(clip_kwargs, self._job_cancel_event)
        worker.moveToThread(self._clip_thread)
        self._clip_thread.started.connect(worker.run)
        worker.finished_ok.connect(self._on_clip_finished)
        worker.failed.connect(self._on_clip_failed)
        worker.progress.connect(self._on_encode_progress)
        worker.finished_ok.connect(self._clip_thread.quit)
        worker.failed.connect(self._clip_thread.quit)
        worker.finished_ok.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        self._clip_thread.finished.connect(self._clip_thread.deleteLater)
        self._clip_thread.start()

    def _on_remove_incomplete_downloads(self) -> None:
        from utils.downloads_cleanup import delete_paths, incomplete_download_paths

        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        paths = incomplete_download_paths(DOWNLOADS)
        if not paths:
            QMessageBox.information(
                self,
                "Incomplete downloads",
                f"No incomplete fragment files were found in:\n{DOWNLOADS.resolve()}",
            )
            return
        preview = "\n".join(p.name for p in paths[:30])
        if len(paths) > 30:
            preview += f"\n… and {len(paths) - 30} more"
        ans = QMessageBox.question(
            self,
            "Remove incomplete downloads?",
            f"Delete {len(paths)} file(s) from the downloads folder?\n\n{preview}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        n, errors = delete_paths(paths)
        msg = f"Removed {n} file(s)."
        if errors:
            msg += "\n\nSome files could not be deleted:\n" + "\n".join(errors[:10])
        self.statusBar().showMessage(msg.replace("\n", " — "), 8000)
        QMessageBox.information(self, "Incomplete downloads", msg)

    def _on_whisper_progress(self, value: float, msg: str) -> None:
        if value < 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(value * 100))
        self.progress.setFormat(msg[:100] if msg else "Transcribing…")

    def _on_whisper_finished(self, path: object) -> None:
        self._set_encode_busy(False, "Ready")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        if isinstance(path, Path):
            self.controls.set_subtitle_path(path)
            self._persist_session_fields()
            self.statusBar().showMessage(f"Transcription saved: {path.name}", 8000)

    def _on_whisper_failed(self, message: str) -> None:
        self._set_encode_busy(False, "Ready")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        if message == "Cancelled":
            self.statusBar().showMessage("Transcription cancelled.", 4000)
            return
        QMessageBox.critical(self, "Whisper failed", message)

    def _on_whisper_transcribe(self) -> None:
        from services.whisper_srt import whisper_cli_path

        if not self._sermon_path or not self._sermon_path.is_file():
            QMessageBox.warning(self, "No sermon", "Open or download a sermon video first.")
            return
        if not whisper_cli_path():
            QMessageBox.information(
                self,
                "Whisper not found",
                "The `whisper` command was not found on PATH.\n\n"
                "Install with:\n  pip install openai-whisper\n\n"
                "FFmpeg is also required for decoding. See README for details.",
            )
            return
        if self._download_in_progress or self._encode_in_progress:
            QMessageBox.information(
                self,
                "Busy",
                "Finish the current download or encode job before running Whisper.",
            )
            return
        if self._whisper_thread and self._whisper_thread.isRunning():
            return

        models = ["tiny", "base", "small", "medium", "large-v2", "large-v3"]
        model, ok = QInputDialog.getItem(
            self,
            "Whisper model",
            "Larger models are slower but more accurate:",
            models,
            0,
            False,
        )
        if not ok:
            return

        lang_raw, ok = QInputDialog.getText(
            self,
            "Whisper language",
            "Optional language code (e.g. en). Leave empty for auto-detect:",
        )
        if not ok:
            return
        lang = lang_raw.strip() or None

        self._job_cancel_event.clear()
        self._set_encode_busy(True, "Transcribing…")
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        self.progress.setFormat("Transcribing…")

        self._whisper_thread = QThread()
        worker = _WhisperWorker(self._sermon_path, model, lang, self._job_cancel_event)
        worker.moveToThread(self._whisper_thread)
        self._whisper_thread.started.connect(worker.run)
        worker.finished_ok.connect(self._on_whisper_finished)
        worker.failed.connect(self._on_whisper_failed)
        worker.progress.connect(self._on_whisper_progress)
        worker.finished_ok.connect(self._whisper_thread.quit)
        worker.failed.connect(self._whisper_thread.quit)
        worker.finished_ok.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        self._whisper_thread.finished.connect(self._whisper_thread.deleteLater)
        self._whisper_thread.start()

    def _on_cancel_job(self) -> None:
        if self._download_in_progress:
            self._download_cancel_event.set()
            return
        self._job_cancel_event.set()

    def _offer_open_containing_folder(self, title: str, path: Path) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"Saved to:\n{path}")
        open_btn = box.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
        ok_btn = box.addButton(QMessageBox.StandardButton.Ok)
        box.setDefaultButton(ok_btn)
        box.exec()
        if box.clickedButton() == open_btn:
            folder = path.parent if path.is_file() else path
            folder.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))

    def _on_encode_progress(self, ratio: float, msg: str) -> None:
        self.progress.setFormat(msg)
        self.progress.setValue(int(max(0.0, min(1.0, ratio)) * 100))

    def _on_export_finished(self, path: object) -> None:
        self._set_encode_busy(False, "Ready")
        self.progress.setValue(100)
        self._qsettings.setValue(KEY_LAST_OUTPUT_STEM, self.controls.output_stem())
        self._persist_session_fields()
        if isinstance(path, Path):
            self._offer_open_containing_folder("Export complete", path)

    def _on_export_failed(self, message: str) -> None:
        self._set_encode_busy(False, "Ready")
        self.progress.setValue(0)
        if message == "Cancelled":
            self.progress.setFormat("Ready")
            return
        self._show_ffmpeg_error_dialog("Export failed", message)

    def _on_clip_failed(self, message: str) -> None:
        self._set_encode_busy(False, "Ready")
        self.progress.setValue(0)
        if message == "Cancelled":
            self.progress.setFormat("Ready")
            return
        self._show_ffmpeg_error_dialog("Clip export failed", message)

    def _show_ffmpeg_error_dialog(self, title: str, message: str) -> None:
        marker = "\n\nLast FFmpeg log lines:\n"
        if marker in message:
            summary, _, detail = message.partition(marker)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle(title)
            box.setText(summary.strip())
            box.setDetailedText(detail.strip())
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.exec()
        else:
            QMessageBox.critical(self, title, message)

    def _on_clip_finished(self, path: object) -> None:
        self._set_encode_busy(False, "Ready")
        self.progress.setValue(100)
        self._persist_session_fields()
        if isinstance(path, Path):
            self._offer_open_containing_folder("Clip saved", path)

    def _sync_trim_preview(self) -> None:
        if not self._sermon_path:
            self.preview.clear_trim_window()
            return
        try:
            spec = self._resolved_trim_spec()
        except ValueError:
            self.preview.clear_trim_window()
            return
        start_ms = int(spec.start_seconds * 1000)
        end_ms = int(spec.end_seconds * 1000)
        self.preview.set_trim_window_ms(start_ms, end_ms)
        self.preview.seek_trim_start()
