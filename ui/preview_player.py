from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal, QTimer
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget


class _ClickableVideoWidget(QVideoWidget):
    """Video surface that toggles play/pause on click."""

    toggle_requested = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.toggle_requested.emit()
        super().mousePressEvent(event)


class PreviewPlayer(QWidget):
    """Embedded sermon preview with play/pause and seek."""

    position_changed_ms = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._trim_start_ms = 0
        self._trim_end_ms = 0
        self._loop_timer = QTimer(self)
        self._loop_timer.setInterval(120)
        self._loop_timer.timeout.connect(self._enforce_trim_window)

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._video = _ClickableVideoWidget(self)
        self._video.toggle_requested.connect(self.toggle_play_pause)
        self._player.setVideoOutput(self._video)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.sliderMoved.connect(self._on_slider_moved)

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)

        self._status = QLabel("No video loaded")
        self._status.setObjectName("PreviewStatus")

        self._play = QPushButton("Play")
        self._pause = QPushButton("Pause")
        self._play.clicked.connect(self._player.play)
        self._pause.clicked.connect(self._player.pause)

        controls = QHBoxLayout()
        controls.addWidget(self._play)
        controls.addWidget(self._pause)
        controls.addStretch()

        layout = QVBoxLayout(self)
        layout.addWidget(self._video, stretch=1)
        layout.addLayout(controls)
        layout.addWidget(self._slider)
        layout.addWidget(self._status)

    def toggle_play_pause(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def set_trim_window_ms(self, start_ms: int, end_ms: int) -> None:
        self._trim_start_ms = max(0, start_ms)
        self._trim_end_ms = max(self._trim_start_ms + 1, end_ms)
        self._loop_timer.start()

    def clear_trim_window(self) -> None:
        self._trim_start_ms = 0
        self._trim_end_ms = 0
        self._loop_timer.stop()

    def _enforce_trim_window(self) -> None:
        if self._trim_end_ms <= self._trim_start_ms:
            return
        pos = self._player.position()
        if self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            return
        if pos >= self._trim_end_ms - 50:
            self._player.setPosition(self._trim_start_ms)

    def load_file(self, path: Path) -> None:
        if not path.exists():
            self._status.setText("File not found")
            return
        url = QUrl.fromLocalFile(str(path.resolve()))
        self._player.setSource(url)
        self._status.setText(path.name)

    def clear(self) -> None:
        self._player.stop()
        self._player.setSource(QUrl())
        self._slider.setRange(0, 0)
        self._status.setText("No video loaded")

    def play(self) -> None:
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def seek_trim_start(self) -> None:
        if self._trim_start_ms:
            self._player.setPosition(self._trim_start_ms)
        else:
            self._player.setPosition(0)

    def _on_slider_moved(self, value: int) -> None:
        self._player.setPosition(value)

    def _on_position_changed(self, pos: int) -> None:
        if self._slider.isSliderDown():
            return
        self._slider.blockSignals(True)
        self._slider.setValue(pos)
        self._slider.blockSignals(False)
        self.position_changed_ms.emit(pos)

    def _on_duration_changed(self, duration: int) -> None:
        self._slider.setRange(0, max(0, duration))
