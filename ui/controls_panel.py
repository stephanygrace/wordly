from __future__ import annotations

from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from utils.timecode import format_timecode, parse_timecode


class ControlsPanel(QWidget):
    """Sermon source, timing, verse, audio, and export fields."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # --- Sermon source ---
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://www.facebook.com/...")

        self.download_btn = QPushButton("Download")
        self.open_local_btn = QPushButton("Open local file…")

        src_row = QHBoxLayout()
        src_row.addWidget(self.url_edit, stretch=1)
        src_row.addWidget(self.download_btn)
        src_row.addWidget(self.open_local_btn)

        src_box = QGroupBox("Sermon source")
        src_layout = QVBoxLayout(src_box)
        src_layout.addLayout(src_row)

        # --- Clip timing ---
        self.start_edit = QLineEdit()
        self.start_edit.setPlaceholderText("00:00:00")
        self.end_edit = QLineEdit()
        self.end_edit.setPlaceholderText("00:05:00")

        timing_box = QGroupBox("Clip timing")
        timing_layout = QVBoxLayout(timing_box)

        timing_layout.addWidget(QLabel("Start time"))
        timing_layout.addLayout(self._timing_row(self.start_edit, self._nudge_start))

        timing_layout.addWidget(QLabel("End time"))
        timing_layout.addLayout(self._timing_row(self.end_edit, self._nudge_end))

        # --- Verse ---
        self.verse_ref = QLineEdit()
        self.verse_ref.setPlaceholderText("John 3:16")
        self.verse_text = QPlainTextEdit()
        self.verse_text.setPlaceholderText("For God so loved the world…")
        self.verse_text.setFixedHeight(88)

        verse_box = QGroupBox("Verse overlay")
        verse_form = QFormLayout(verse_box)
        verse_form.addRow("Reference", self.verse_ref)
        verse_form.addRow("Verse text", self.verse_text)

        # --- Audio ---
        self.sermon_vol = QSlider(Qt.Orientation.Horizontal)
        self.sermon_vol.setRange(0, 100)
        self.sermon_vol.setValue(100)

        self.piano_vol = QSlider(Qt.Orientation.Horizontal)
        self.piano_vol.setRange(0, 100)
        self.piano_vol.setValue(35)

        self.fade_in = QCheckBox("Fade in (music)")
        self.fade_out = QCheckBox("Fade out (music)")

        self.music_path = QLineEdit()
        self.music_path.setReadOnly(True)
        self.music_browse = QPushButton("Choose MP3…")

        def browse_music() -> None:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Choose piano / instrumental track",
                "",
                "Audio (*.mp3 *.wav *.m4a);;All files (*)",
            )
            if path:
                self.music_path.setText(path)

        self.music_browse.clicked.connect(browse_music)

        audio_box = QGroupBox("Audio controls")
        audio_layout = QVBoxLayout(audio_box)
        audio_layout.addWidget(QLabel("Sermon volume"))
        audio_layout.addWidget(self.sermon_vol)
        audio_layout.addWidget(QLabel("Piano / bed volume"))
        audio_layout.addWidget(self.piano_vol)
        row_fade = QHBoxLayout()
        row_fade.addWidget(self.fade_in)
        row_fade.addWidget(self.fade_out)
        audio_layout.addLayout(row_fade)
        music_row = QHBoxLayout()
        music_row.addWidget(self.music_path, stretch=1)
        music_row.addWidget(self.music_browse)
        audio_layout.addWidget(QLabel("Background music file"))
        audio_layout.addLayout(music_row)

        # --- Export ---
        self.output_name = QLineEdit()
        self.output_name.setPlaceholderText("sunday-highlight-reel")
        self.export_btn = QPushButton("Export reel")

        export_box = QGroupBox("Export")
        export_layout = QVBoxLayout(export_box)
        export_layout.addWidget(QLabel("Output filename (without extension)"))
        export_layout.addWidget(self.output_name)
        export_layout.addWidget(self.export_btn)

        root = QVBoxLayout(self)
        root.addWidget(src_box)
        root.addWidget(timing_box)
        root.addWidget(verse_box)
        root.addWidget(audio_box)
        root.addWidget(export_box)
        root.addStretch()

    def _timing_row(self, field: QLineEdit, nudge_cb) -> QHBoxLayout:  # noqa: ANN001
        row = QHBoxLayout()
        row.addWidget(field, stretch=1)
        for label, delta in (("-5s", -5), ("-1s", -1), ("+1s", 1), ("+5s", 5)):
            btn = QPushButton(label)
            btn.clicked.connect(partial(nudge_cb, field, delta))
            row.addWidget(btn)
        return row

    def _nudge_start(self, field: QLineEdit, delta: int) -> None:
        self._nudge_field(field, delta)

    def _nudge_end(self, field: QLineEdit, delta: int) -> None:
        self._nudge_field(field, delta)

    @staticmethod
    def _nudge_field(field: QLineEdit, delta: int) -> None:
        text = field.text().strip() or "00:00:00"
        try:
            base = parse_timecode(text).total_seconds
        except ValueError:
            base = 0.0
        field.setText(format_timecode(max(0.0, base + float(delta))))

    def facebook_url(self) -> str:
        return self.url_edit.text().strip()

    def start_text(self) -> str:
        return self.start_edit.text().strip()

    def end_text(self) -> str:
        return self.end_edit.text().strip()

    def verse_reference(self) -> str:
        return self.verse_ref.text().strip()

    def verse_body(self) -> str:
        return self.verse_text.toPlainText().strip()

    def sermon_volume(self) -> int:
        return int(self.sermon_vol.value())

    def piano_volume(self) -> int:
        return int(self.piano_vol.value())

    def piano_fade_in(self) -> bool:
        return self.fade_in.isChecked()

    def piano_fade_out(self) -> bool:
        return self.fade_out.isChecked()

    def piano_file(self) -> Path | None:
        text = self.music_path.text().strip()
        return Path(text) if text else None

    def output_stem(self) -> str:
        stem = self.output_name.text().strip() or "wordly-export"
        for ch in '<>:"/\\|?*':
            stem = stem.replace(ch, "_")
        return stem
