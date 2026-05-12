from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget

from utils.timecode import format_timecode


def _format_ms_as_timecode(ms: int) -> str:
    if ms < 0:
        ms = 0
    return format_timecode(ms / 1000.0)


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
        self._slider.setToolTip("Drag to seek; use arrow keys when the slider has focus")
        self._slider.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._slider.sliderMoved.connect(self._on_slider_moved)

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)

        self._time_label = QLabel("00:00:00 / --:--:--")
        self._time_label.setObjectName("PreviewTimecode")

        self._status = QLabel("No video loaded")
        self._status.setObjectName("PreviewStatus")

        self._play_toggle = QPushButton("Play")
        self._play_toggle.setMinimumWidth(92)
        self._play_toggle.setToolTip(
            "Play or pause the preview (click the video, or press Space when the preview area is focused)"
        )
        self._play_toggle.clicked.connect(self.toggle_play_pause)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)

        self._jump_start = QPushButton("Jump to clip start")
        self._jump_end = QPushButton("Jump to clip end")
        self._jump_start.setToolTip("Seek to the current Start time")
        self._jump_end.setToolTip("Seek near the current End time")
        self._jump_start.clicked.connect(self.seek_trim_start)
        self._jump_end.clicked.connect(self.seek_trim_end)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        controls.addWidget(self._play_toggle)
        controls.addWidget(self._jump_start)
        controls.addWidget(self._jump_end)
        controls.addStretch()

        self._hint = QLabel("Tip: click the video for play/pause, or focus this panel and press Space")
        self._hint.setObjectName("PreviewHint")
        self._hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addWidget(self._video, stretch=1)
        layout.addLayout(controls)
        layout.addWidget(self._time_label)
        layout.addWidget(self._slider)
        layout.addWidget(self._status)
        layout.addWidget(self._hint)

        self._video.setMinimumHeight(220)

    def _on_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._play_toggle.setText("Pause")
        else:
            self._play_toggle.setText("Play")

    def attach_space_shortcut(self, shortcut_parent: QWidget) -> None:
        """Play/pause when this widget tree has focus (e.g. preview panel)."""
        sc = QShortcut(QKeySequence(Qt.Key.Key_Space), shortcut_parent)
        sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc.activated.connect(self.toggle_play_pause)

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
        self._update_time_label(0, max(0, self._player.duration()))
        self._on_playback_state_changed(self._player.playbackState())

    def clear(self) -> None:
        self._player.stop()
        self._player.setSource(QUrl())
        self._slider.setRange(0, 0)
        self._status.setText("No video loaded")
        self._time_label.setText("00:00:00 / --:--:--")
        self._on_playback_state_changed(self._player.playbackState())

    def play(self) -> None:
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def seek_trim_start(self) -> None:
        if self._trim_start_ms or self._trim_end_ms > 0:
            self._player.setPosition(self._trim_start_ms)
        else:
            self._player.setPosition(0)

    def seek_trim_end(self) -> None:
        if self._trim_end_ms <= self._trim_start_ms:
            return
        # Land slightly before the end so the loop timer does not immediately rewind.
        self._player.setPosition(max(self._trim_start_ms, self._trim_end_ms - 400))

    def _on_slider_moved(self, value: int) -> None:
        self._player.setPosition(value)
        self._update_time_label(value, None)

    def _on_position_changed(self, pos: int) -> None:
        if self._slider.isSliderDown():
            return
        self._slider.blockSignals(True)
        self._slider.setValue(pos)
        self._slider.blockSignals(False)
        self.position_changed_ms.emit(pos)
        self._update_time_label(pos, None)

    def _on_duration_changed(self, duration: int) -> None:
        self._slider.setRange(0, max(0, duration))
        self._update_time_label(None, duration)

    def _update_time_label(self, pos_ms: int | None, dur_ms: int | None) -> None:
        dur = self._slider.maximum() if dur_ms is None else dur_ms
        pos = self._player.position() if pos_ms is None else pos_ms
        if dur <= 0:
            self._time_label.setText(f"{_format_ms_as_timecode(pos)} / --:--:--")
        else:
            self._time_label.setText(f"{_format_ms_as_timecode(pos)} / {_format_ms_as_timecode(dur)}")
