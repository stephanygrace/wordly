from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from services.downloader import download_facebook_video
from services.renderer import render_vertical_reel
from services.trimmer import parse_trim_times
from ui.controls_panel import ControlsPanel
from ui.preview_player import PreviewPlayer
from utils.paths import EXPORTS, ensure_directories


class _DownloadWorker(QObject):
    finished_ok = Signal(object)
    failed = Signal(str)
    progress = Signal(float, str)

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url

    def run(self) -> None:
        try:
            path = download_facebook_video(
                self._url,
                progress_cb=lambda ratio, msg: self.progress.emit(ratio, msg),
            )
        except Exception as exc:  # noqa: BLE001 - surface to UI
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(path)


class _ExportWorker(QObject):
    finished_ok = Signal(object)
    failed = Signal(str)
    progress = Signal(float, str)

    def __init__(self, kwargs: dict) -> None:
        super().__init__()
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            out = render_vertical_reel(**self._kwargs, progress_cb=lambda r, m: self.progress.emit(r, m))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(out)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        ensure_directories()
        self.setWindowTitle("Wordly — Sermon highlight production")
        self.resize(1280, 820)

        self._sermon_path: Path | None = None
        self._download_thread: QThread | None = None
        self._export_thread: QThread | None = None

        self.controls = ControlsPanel()
        self.preview = PreviewPlayer()

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("Idle")

        preview_frame = QFrame()
        preview_frame.setObjectName("PreviewFrame")
        pv_layout = QVBoxLayout(preview_frame)
        pv_layout.addWidget(QLabel("Video preview"))
        pv_layout.addWidget(self.preview, stretch=1)
        pv_layout.addWidget(self.progress)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.controls)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(scroll)
        splitter.addWidget(preview_frame)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 880])

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addWidget(splitter)
        self.setCentralWidget(central)

        self._apply_styles()

        self.controls.download_btn.clicked.connect(self._on_download)
        self.controls.open_local_btn.clicked.connect(self._on_open_local)
        self.controls.export_btn.clicked.connect(self._on_export)

        self.controls.start_edit.editingFinished.connect(self._sync_trim_preview)
        self.controls.end_edit.editingFinished.connect(self._sync_trim_preview)

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
            QLineEdit, QProgressBar {
                border: 1px solid #3c424c;
                border-radius: 8px;
                padding: 6px 8px;
                background-color: #121418;
                selection-background-color: #3d5afe;
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
            QLabel#PreviewStatus {
                color: #9aa0a6;
            }
            QScrollArea { border: none; }
            """
        )
        self.controls.download_btn.setObjectName("AccentButton")
        self.controls.export_btn.setObjectName("AccentButton")

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.controls.download_btn.setEnabled(not busy)
        self.controls.export_btn.setEnabled(not busy)
        if message:
            self.progress.setFormat(message)

    def _on_download(self) -> None:
        url = self.controls.facebook_url()
        if not url:
            QMessageBox.warning(self, "Missing URL", "Paste a Facebook sermon or live video URL.")
            return
        if self._download_thread and self._download_thread.isRunning():
            return

        self._set_busy(True, "Downloading…")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        self._download_thread = QThread()
        worker = _DownloadWorker(url)
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
        self._set_busy(False, "Ready")
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        if isinstance(path, Path):
            self._load_sermon(path)

    def _on_download_failed(self, message: str) -> None:
        self._set_busy(False, "Ready")
        self.progress.setValue(0)
        QMessageBox.critical(self, "Download failed", message)

    def _on_open_local(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Open sermon video",
            "",
            "Video (*.mp4 *.mov *.mkv *.webm);;All files (*)",
        )
        if path_str:
            self._load_sermon(Path(path_str))

    def _load_sermon(self, path: Path) -> None:
        self._sermon_path = path
        self.preview.load_file(path)
        self._sync_trim_preview()

    def _sync_trim_preview(self) -> None:
        if not self._sermon_path:
            self.preview.clear_trim_window()
            return
        try:
            spec = parse_trim_times(self.controls.start_text(), self.controls.end_text())
        except ValueError:
            self.preview.clear_trim_window()
            return
        start_ms = int(spec.start_seconds * 1000)
        end_ms = int(spec.end_seconds * 1000)
        self.preview.set_trim_window_ms(start_ms, end_ms)
        self.preview.seek_trim_start()

    def _on_export(self) -> None:
        if not self._sermon_path or not self._sermon_path.exists():
            QMessageBox.warning(self, "No sermon", "Download or open a sermon video first.")
            return
        music = self.controls.piano_file()
        if not music or not music.exists():
            QMessageBox.warning(self, "Music", "Choose a local MP3 (or WAV/M4A) for the piano bed.")
            return
        try:
            spec = parse_trim_times(self.controls.start_text(), self.controls.end_text())
        except ValueError as exc:
            QMessageBox.warning(self, "Timing", str(exc))
            return

        out = EXPORTS / f"{self.controls.output_stem()}.mp4"
        if self._export_thread and self._export_thread.isRunning():
            return

        self._set_busy(True, "Rendering…")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        kwargs = dict(
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
        )

        self._export_thread = QThread()
        worker = _ExportWorker(kwargs)
        worker.moveToThread(self._export_thread)
        self._export_thread.started.connect(worker.run)
        worker.finished_ok.connect(self._on_export_finished)
        worker.failed.connect(self._on_export_failed)
        worker.progress.connect(self._on_export_progress)
        worker.finished_ok.connect(self._export_thread.quit)
        worker.failed.connect(self._export_thread.quit)
        worker.finished_ok.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        self._export_thread.finished.connect(self._export_thread.deleteLater)
        self._export_thread.start()

    def _on_export_progress(self, ratio: float, msg: str) -> None:
        self.progress.setFormat(msg)
        self.progress.setValue(int(max(0.0, min(1.0, ratio)) * 100))

    def _on_export_finished(self, path: object) -> None:
        self._set_busy(False, "Ready")
        self.progress.setValue(100)
        if isinstance(path, Path):
            QMessageBox.information(self, "Export complete", f"Saved to:\n{path}")

    def _on_export_failed(self, message: str) -> None:
        self._set_busy(False, "Ready")
        self.progress.setValue(0)
        QMessageBox.critical(self, "Export failed", message)
