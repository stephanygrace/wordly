from __future__ import annotations

import os
import platform
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QStackedLayout, QVBoxLayout, QWidget

from services.ffmpeg_frame import extract_preview_frame
from services.trimmer import ffprobe_duration_seconds
from utils.console_log import log_info, log_warn
from utils.timecode import format_timecode


def _format_ms_as_timecode(ms: int) -> str:
    if ms < 0:
        ms = 0
    return format_timecode(ms / 1000.0)


def _running_on_wsl() -> bool:
    return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().lower()


class PreviewPlayer(QWidget):
    """Sermon preview with Qt multimedia playback and FFmpeg scrub fallback."""

    position_changed_ms = Signal(int)
    _frame_ready = Signal(QPixmap, int, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._path: Path | None = None
        self._duration_ms = 0
        self._position_ms = 0
        self._trim_start_ms = 0
        self._trim_end_ms = 0
        self._scrubbing = False
        self._ffmpeg_fallback = False
        self._frame_request_id = 0

        self._stack_host = QWidget()
        self._stack = QStackedLayout(self._stack_host)
        self._video = QVideoWidget()
        self._video.setMinimumHeight(220)
        self._frame = QLabel("No video loaded")
        self._frame.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._frame.setMinimumHeight(220)
        self._frame.setStyleSheet("background: #111; color: #aaa;")
        self._stack.addWidget(self._video)
        self._stack.addWidget(self._frame)

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video)
        self._player.positionChanged.connect(self._on_player_position)
        self._player.durationChanged.connect(self._on_player_duration)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.errorOccurred.connect(self._on_player_error)

        self._seek_timer = QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(60)
        self._seek_timer.timeout.connect(self._flush_pending_seek)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.setToolTip("Drag to scrub; release to seek")
        self._slider.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._slider.sliderPressed.connect(self._on_slider_pressed)
        self._slider.sliderMoved.connect(self._on_slider_moved)
        self._slider.sliderReleased.connect(self._on_slider_released)

        self._time_label = QLabel("00:00:00 / --:--:--")
        self._status = QLabel("No video loaded")
        self._status.setWordWrap(True)

        self._play_toggle = QPushButton("Play")
        self._play_toggle.setObjectName("PreviewControlButton")
        self._play_toggle.setMinimumWidth(92)
        self._play_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play_toggle.clicked.connect(self.toggle_play_pause)

        self._jump_start = QPushButton("Jump to clip start")
        self._jump_end = QPushButton("Jump to clip end")
        for btn in (self._jump_start, self._jump_end):
            btn.setObjectName("PreviewControlButton")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._jump_start.clicked.connect(self.seek_trim_start)
        self._jump_end.clicked.connect(self.seek_trim_end)

        controls = QHBoxLayout()
        controls.addWidget(self._play_toggle)
        controls.addWidget(self._jump_start)
        controls.addWidget(self._jump_end)
        controls.addStretch()

        self._hint = QLabel("Smooth playback via Qt Multimedia. Scrub the slider to seek within the clip.")
        self._hint.setObjectName("MutedHelpLabel")
        self._hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self._stack_host, stretch=1)
        layout.addLayout(controls)
        layout.addWidget(self._time_label)
        layout.addWidget(self._slider)
        layout.addWidget(self._status)
        layout.addWidget(self._hint)

        self._frame_ready.connect(self._apply_ffmpeg_frame)

    def attach_space_shortcut(self, shortcut_parent: QWidget) -> None:
        sc = QShortcut(QKeySequence(Qt.Key.Key_Space), shortcut_parent)
        sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc.activated.connect(self.toggle_play_pause)

    def toggle_play_pause(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self.play()

    def set_trim_window_ms(self, start_ms: int, end_ms: int) -> None:
        self._trim_start_ms = max(0, start_ms)
        self._trim_end_ms = max(self._trim_start_ms + 1, end_ms)
        self._apply_slider_range()
        if self._path:
            self.seek_trim_start()

    def clear_trim_window(self) -> None:
        self._trim_start_ms = 0
        self._trim_end_ms = 0
        self._apply_slider_range()

    def load_file(self, path: Path) -> None:
        if not path.exists():
            self._status.setText("File not found")
            return
        self._player.stop()
        self._path = path.resolve()
        self._ffmpeg_fallback = False
        self._stack.setCurrentWidget(self._video)
        try:
            self._duration_ms = int(ffprobe_duration_seconds(self._path) * 1000)
        except Exception as exc:  # noqa: BLE001
            self._status.setText(str(exc))
            log_warn("preview", str(exc))
            return
        self._apply_slider_range()
        self._player.setSource(QUrl.fromLocalFile(str(self._path)))
        start = self._trim_start_ms if self._trim_end_ms > self._trim_start_ms else 0
        self._seek_to(start)
        self._status.setText(f"Loaded {path.name}")
        log_info("preview", f"Loaded {path.name} ({_format_ms_as_timecode(self._duration_ms)})")
        if _running_on_wsl():
            self._enable_ffmpeg_preview(
                "WSL preview uses FFmpeg frame scrub — Qt video output is unreliable here."
            )
            QTimer.singleShot(0, lambda: self._show_ffmpeg_frame(self._position_ms))

    def clear(self) -> None:
        self._player.stop()
        self._path = None
        self._duration_ms = 0
        self._position_ms = 0
        self._slider.setRange(0, 0)
        self._stack.setCurrentWidget(self._frame)
        self._frame.setText("No video loaded")
        self._frame.setPixmap(QPixmap())
        self._status.setText("No video loaded")
        self._time_label.setText("00:00:00 / --:--:--")

    def play(self) -> None:
        if not self._path:
            return
        if self._trim_end_ms > self._trim_start_ms and self._position_ms >= self._trim_end_ms - 200:
            self._seek_to(self._trim_start_ms)
        if self._ffmpeg_fallback:
            self._status.setText("FFmpeg fallback cannot play video — scrub the slider to preview frames.")
            return
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def seek_trim_start(self) -> None:
        target = self._trim_start_ms if self._trim_end_ms > self._trim_start_ms else 0
        self._seek_to(target)

    def seek_trim_end(self) -> None:
        if self._trim_end_ms <= self._trim_start_ms:
            return
        self._seek_to(max(self._trim_start_ms, self._trim_end_ms - 400))

    def _apply_slider_range(self) -> None:
        if self._trim_end_ms > self._trim_start_ms:
            self._slider.setRange(self._trim_start_ms, self._trim_end_ms)
        else:
            self._slider.setRange(0, max(0, self._duration_ms))

    def _on_slider_pressed(self) -> None:
        self._scrubbing = True
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()

    def _on_slider_moved(self, value: int) -> None:
        self._position_ms = value
        self._update_time_label(value)
        if self._ffmpeg_fallback:
            self._show_ffmpeg_frame(value)

    def _on_slider_released(self) -> None:
        self._scrubbing = False
        self._seek_to(self._slider.value())

    def _flush_pending_seek(self) -> None:
        pass

    def _seek_to(self, ms: int) -> None:
        if not self._path:
            return
        ms = self._clamp_position(ms)
        self._position_ms = ms
        self._slider.blockSignals(True)
        self._slider.setValue(ms)
        self._slider.blockSignals(False)
        self._update_time_label(ms)
        self.position_changed_ms.emit(ms)
        if self._ffmpeg_fallback:
            self._show_ffmpeg_frame(ms)
        else:
            self._player.setPosition(ms)

    def _clamp_position(self, ms: int) -> int:
        ms = max(0, min(ms, self._duration_ms))
        if self._trim_end_ms > self._trim_start_ms:
            ms = max(self._trim_start_ms, min(ms, self._trim_end_ms))
        return ms

    @Slot("qint64")
    def _on_player_position(self, pos_ms: int) -> None:
        if self._scrubbing:
            return
        pos_ms = int(pos_ms)
        if self._trim_end_ms > self._trim_start_ms and pos_ms >= self._trim_end_ms - 80:
            self._player.setPosition(self._trim_start_ms)
            return
        self._position_ms = pos_ms
        self._slider.blockSignals(True)
        self._slider.setValue(pos_ms)
        self._slider.blockSignals(False)
        self._update_time_label(pos_ms)
        self.position_changed_ms.emit(pos_ms)

    @Slot("qint64")
    def _on_player_duration(self, duration_ms: int) -> None:
        if duration_ms > 0:
            self._duration_ms = int(duration_ms)
            self._apply_slider_range()

    @Slot(QMediaPlayer.PlaybackState)
    def _on_playback_state(self, state: QMediaPlayer.PlaybackState) -> None:
        self._play_toggle.setText(
            "Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play"
        )

    @Slot(QMediaPlayer.Error, str)
    def _on_player_error(self, error: QMediaPlayer.Error, message: str) -> None:  # noqa: ARG002
        if self._ffmpeg_fallback:
            return
        self._enable_ffmpeg_preview(
            f"Qt playback unavailable ({message}) — scrub the slider to preview frames."
        )

    def _enable_ffmpeg_preview(self, status: str) -> None:
        self._ffmpeg_fallback = True
        self._stack.setCurrentWidget(self._frame)
        self._player.stop()
        self._hint.setText("Frame preview via FFmpeg. Scrub the slider or use jump buttons to seek.")
        self._status.setText(status)
        log_warn("preview", status)

    def _show_ffmpeg_frame(self, ms: int) -> None:
        if not self._path:
            return
        self._frame_request_id += 1
        request_id = self._frame_request_id
        path = self._path
        target = self._frame.size()
        if target.width() < 8 or target.height() < 8:
            target = self._stack_host.size()

        def work() -> None:
            try:
                frame_path = extract_preview_frame(path, ms / 1000.0)
                if request_id != self._frame_request_id:
                    return
                pix = QPixmap(str(frame_path))
                if pix.isNull():
                    return
                scaled = pix.scaled(
                    target,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._frame_ready.emit(scaled, ms, f"Preview @ {_format_ms_as_timecode(ms)}")
            except Exception as exc:  # noqa: BLE001
                if request_id == self._frame_request_id:
                    self._frame_ready.emit(QPixmap(), ms, f"Preview error: {exc}")

        threading.Thread(target=work, daemon=True).start()

    @Slot(object, int, str)
    def _apply_ffmpeg_frame(self, pixmap: QPixmap, ms: int, status: str) -> None:
        if pixmap.isNull():
            self._status.setText(status)
            return
        self._frame.setPixmap(pixmap)
        self._frame.setText("")
        self._status.setText(status)
        self._update_time_label(ms)

    def _update_time_label(self, pos_ms: int) -> None:
        dur = self._duration_ms
        if dur <= 0:
            self._time_label.setText(f"{_format_ms_as_timecode(pos_ms)} / --:--:--")
        else:
            self._time_label.setText(f"{_format_ms_as_timecode(pos_ms)} / {_format_ms_as_timecode(dur)}")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._ffmpeg_fallback and self._path:
            self._show_ffmpeg_frame(self._position_ms)
