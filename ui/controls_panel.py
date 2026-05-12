from __future__ import annotations

from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QSettings, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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

from utils.app_settings import (
    KEY_LAST_COOKIES_DIR,
    KEY_LAST_COOKIES_FILE,
    KEY_LAST_MUSIC_DIR,
    KEY_LAST_MUSIC_FILE,
    KEY_LAST_SUBTITLE_DIR,
    KEY_LAST_SUBTITLE_FILE,
    KEY_LAST_TEMPLATE_JSON,
    KEY_LAST_VERSE_REF,
    KEY_LAST_VERSE_TEXT,
)
from utils.layout_template import list_template_files, load_layout
from utils.paths import CLIPS, DOWNLOADS, EXPORTS
from utils.timecode import format_timecode, parse_timecode

_MAX_VERSE_BODY_CHARS = 32_000


class ControlsPanel(QWidget):
    """Sermon source, timing, verse, audio, and export fields."""

    timings_changed = Signal()
    set_end_to_file_end_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings: QSettings | None = None
        self._nudge_buttons: list[QPushButton] = []
        self._file_duration_known = False
        self._lock_downloading = False
        self._lock_encoding = False

        # --- Sermon source ---
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://www.facebook.com/...")
        self.url_edit.setToolTip(
            "Paste a Facebook sermon or live video URL, then Download — or press Enter. "
            "The last URL is restored when you reopen Wordly."
        )

        self.download_btn = QPushButton("Download")
        self.download_btn.setToolTip("Download into the project downloads folder (requires yt-dlp).")
        self.open_local_btn = QPushButton("Open local file…")
        self.open_local_btn.setToolTip("Choose a sermon video from disk, or drag a file onto the main window.")

        src_row = QHBoxLayout()
        src_row.addWidget(self.url_edit, stretch=1)
        src_row.addWidget(self.download_btn)
        src_row.addWidget(self.open_local_btn)

        self.cookies_path = QLineEdit()
        self.cookies_path.setReadOnly(True)
        self.cookies_path.setPlaceholderText("Optional — Netscape cookies.txt for yt-dlp (Facebook login)")
        self.cookies_browse = QPushButton("Cookies…")
        self.cookies_browse.setToolTip(
            "Export cookies from your browser (Netscape format) for sites that require a login. "
            "See README → Facebook downloads and cookies."
        )
        self.cookies_clear = QPushButton("Clear")
        self.cookies_clear.setToolTip("Remove the cookies file from this session.")
        self.cookies_browse.clicked.connect(self._browse_cookies)
        self.cookies_clear.clicked.connect(self._clear_cookies)
        cookies_row = QHBoxLayout()
        cookies_row.addWidget(self.cookies_path, stretch=1)
        cookies_row.addWidget(self.cookies_browse)
        cookies_row.addWidget(self.cookies_clear)

        src_box = QGroupBox("Sermon source")
        src_layout = QVBoxLayout(src_box)
        src_layout.addLayout(src_row)
        src_layout.addLayout(cookies_row)

        # --- Clip timing ---
        self.start_edit = QLineEdit()
        self.start_edit.setPlaceholderText("00:00:00")
        self.end_edit = QLineEdit()
        self.end_edit.setPlaceholderText("00:05:00")

        self.duration_label = QLabel("Media duration: —")
        self.duration_label.setObjectName("DurationHint")

        self.end_at_file_btn = QPushButton("Set end → file end")
        self.end_at_file_btn.setToolTip(
            "Set End to the probed duration of the loaded sermon (available after ffprobe succeeds)."
        )
        self.end_at_file_btn.setEnabled(False)
        self.end_at_file_btn.clicked.connect(self.set_end_to_file_end_requested.emit)

        dur_row = QHBoxLayout()
        dur_row.addWidget(self.duration_label, stretch=1)
        dur_row.addWidget(self.end_at_file_btn)

        timing_box = QGroupBox("Clip timing")
        timing_layout = QVBoxLayout(timing_box)
        timing_layout.setSpacing(8)
        timing_layout.addLayout(dur_row)

        timing_layout.addWidget(QLabel("Start time"))
        timing_layout.addLayout(self._timing_row(self.start_edit, self._nudge_start))

        timing_layout.addWidget(QLabel("End time"))
        timing_layout.addLayout(self._timing_row(self.end_edit, self._nudge_end))

        self.start_edit.textChanged.connect(self.timings_changed.emit)
        self.end_edit.textChanged.connect(self.timings_changed.emit)

        # --- Layout template ---
        self.template_combo = QComboBox()
        self.template_combo.setToolTip("JSON templates under templates/ control frame layout and encode quality.")
        self._reload_template_combo()
        self.reload_templates_btn = QPushButton("Reload list")
        self.reload_templates_btn.setToolTip("Rescan the templates folder for new or edited JSON files")
        self.reload_templates_btn.clicked.connect(self.reload_templates)

        template_row = QHBoxLayout()
        template_row.addWidget(self.template_combo, stretch=1)
        template_row.addWidget(self.reload_templates_btn)

        template_box = QGroupBox("Reel layout (template)")
        template_layout = QVBoxLayout(template_box)
        template_layout.setSpacing(8)
        template_layout.addLayout(template_row)

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

        # --- Optional subtitles (burn-in on export) ---
        self.subtitle_path = QLineEdit()
        self.subtitle_path.setReadOnly(True)
        self.subtitle_path.setPlaceholderText("Optional — .srt, .vtt, or .ass / .ssa timed to the full sermon file")
        self.subtitle_browse = QPushButton("Browse…")
        self.subtitle_clear = QPushButton("Clear")
        self.subtitle_browse.clicked.connect(self._browse_subtitle)
        self.subtitle_clear.clicked.connect(self._clear_subtitle)

        sub_row = QHBoxLayout()
        sub_row.addWidget(self.subtitle_path, stretch=1)
        sub_row.addWidget(self.subtitle_browse)
        sub_row.addWidget(self.subtitle_clear)

        sub_box = QGroupBox("Subtitles (optional, reel export)")
        sub_hint = QLabel(
            "SubRip (.srt), WebVTT (.vtt), or ASS/SSA (.ass / .ssa) cues are converted/shifted to match Start/End. "
            "ASS keeps styles and karaoke-style tags (e.g. \\k) for libass; VTT strips simple HTML-like tags. "
            "Text is burned into the bottom band (ASS uses your file’s layout; SRT/VTT use a forced bottom style)."
        )
        sub_hint.setObjectName("MutedHelpLabel")
        sub_hint.setWordWrap(True)
        sub_layout = QVBoxLayout(sub_box)
        sub_layout.setSpacing(8)
        sub_layout.addWidget(sub_hint)
        sub_layout.addLayout(sub_row)

        self.subtitle_browse.setToolTip(
            "Choose a .srt or .vtt timed to the full sermon file (burned in on Export reel only)."
        )
        self.subtitle_clear.setToolTip("Remove the subtitle file from this export.")

        # --- Audio ---
        self.sermon_vol = QSlider(Qt.Orientation.Horizontal)
        self.sermon_vol.setRange(0, 100)
        self.sermon_vol.setValue(100)
        self.sermon_vol_label = QLabel("100%")
        self.sermon_vol_label.setObjectName("VolumePct")
        self.sermon_vol_label.setMinimumWidth(40)
        self.sermon_vol_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.sermon_vol.valueChanged.connect(lambda v: self.sermon_vol_label.setText(f"{v}%"))

        self.piano_vol = QSlider(Qt.Orientation.Horizontal)
        self.piano_vol.setRange(0, 100)
        self.piano_vol.setValue(35)
        self.piano_vol_label = QLabel("35%")
        self.piano_vol_label.setObjectName("VolumePct")
        self.piano_vol_label.setMinimumWidth(40)
        self.piano_vol_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.piano_vol.valueChanged.connect(lambda v: self.piano_vol_label.setText(f"{v}%"))

        self.fade_in = QCheckBox("Fade in (music)")
        self.fade_out = QCheckBox("Fade out (music)")

        self.music_path = QLineEdit()
        self.music_path.setReadOnly(True)
        self.music_browse = QPushButton("Choose MP3…")
        self.music_browse.setToolTip("Background instrumental mixed under the sermon (required for Export reel).")
        self.music_browse.clicked.connect(self._browse_music)
        self.music_clear_btn = QPushButton("Clear")
        self.music_clear_btn.setToolTip("Remove the selected music file from this session")
        self.music_clear_btn.clicked.connect(self._clear_music)
        self.music_clear_btn.setEnabled(False)

        audio_box = QGroupBox("Audio controls")
        audio_layout = QVBoxLayout(audio_box)
        audio_layout.setSpacing(8)
        audio_layout.addWidget(QLabel("Sermon volume"))
        row_sv = QHBoxLayout()
        row_sv.addWidget(self.sermon_vol, stretch=1)
        row_sv.addWidget(self.sermon_vol_label)
        audio_layout.addLayout(row_sv)
        audio_layout.addWidget(QLabel("Piano / bed volume"))
        row_pv = QHBoxLayout()
        row_pv.addWidget(self.piano_vol, stretch=1)
        row_pv.addWidget(self.piano_vol_label)
        audio_layout.addLayout(row_pv)
        row_fade = QHBoxLayout()
        row_fade.addWidget(self.fade_in)
        row_fade.addWidget(self.fade_out)
        audio_layout.addLayout(row_fade)
        music_row = QHBoxLayout()
        music_row.addWidget(self.music_path, stretch=1)
        music_row.addWidget(self.music_browse)
        music_row.addWidget(self.music_clear_btn)
        audio_layout.addWidget(QLabel("Background music (required for reel export)"))
        audio_layout.addLayout(music_row)

        # --- Export ---
        self.output_name = QLineEdit()
        self.output_name.setPlaceholderText("sunday-highlight-reel")
        self.output_name.setToolTip("Base name for exports/wordly-export.mp4 (no extension).")
        self.export_btn = QPushButton("Export reel")
        self.export_btn.setToolTip(
            "Render a vertical 9:16 reel with verse overlay, music mix, and optional subtitles (Ctrl+E)."
        )
        self.save_clip_btn = QPushButton("Save trimmed clip…")
        self.save_clip_btn.setToolTip("Export the horizontal sermon trim only (no reel, music, or subtitles).")
        self.cancel_job_btn = QPushButton("Cancel")
        self.cancel_job_btn.setToolTip("Stop an in-progress download, Whisper transcription, reel export, or clip save.")
        self.cancel_job_btn.setEnabled(False)

        export_box = QGroupBox("Export")
        export_layout = QVBoxLayout(export_box)
        export_layout.setSpacing(8)
        export_layout.addWidget(QLabel("Output filename (without extension)"))
        export_layout.addWidget(self.output_name)
        row_ex = QHBoxLayout()
        row_ex.setSpacing(10)
        row_ex.addWidget(self.export_btn, stretch=2)
        row_ex.addWidget(self.save_clip_btn, stretch=1)
        export_layout.addLayout(row_ex)
        export_layout.addWidget(self.cancel_job_btn)

        self.open_exports_btn = QPushButton("Open exports folder")
        self.open_clips_btn = QPushButton("Open clips folder")
        self.open_downloads_btn = QPushButton("Open downloads folder")
        self.open_exports_btn.clicked.connect(lambda: self._open_project_dir(EXPORTS))
        self.open_clips_btn.clicked.connect(lambda: self._open_project_dir(CLIPS))
        self.open_downloads_btn.clicked.connect(lambda: self._open_project_dir(DOWNLOADS))
        folders_row = QHBoxLayout()
        folders_row.addWidget(self.open_exports_btn)
        folders_row.addWidget(self.open_clips_btn)
        folders_row.addWidget(self.open_downloads_btn)
        export_layout.addLayout(folders_row)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 4, 8)
        root.setSpacing(14)
        root.addWidget(src_box)
        root.addWidget(timing_box)
        root.addWidget(template_box)
        root.addWidget(verse_box)
        root.addWidget(sub_box)
        root.addWidget(audio_box)
        root.addWidget(export_box)
        root.addStretch()

    def _reload_template_combo(self) -> None:
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self.template_combo.addItem("Built-in default", None)
        for p in list_template_files():
            try:
                lay = load_layout(p)
            except (OSError, ValueError, KeyError, TypeError):
                self.template_combo.addItem(p.name, str(p.resolve()))
                continue
            self.template_combo.addItem(f"{lay.name} ({p.name})", str(p.resolve()))
        self.template_combo.blockSignals(False)

    def reload_templates(self) -> None:
        """Rescan ``templates/*.json`` and try to keep the current selection."""
        prev = self.selected_template_path()
        prev_str = str(prev.resolve()) if prev else None
        self._reload_template_combo()
        if prev_str:
            for i in range(self.template_combo.count()):
                data = self.template_combo.itemData(i)
                if data is not None and str(data) == prev_str:
                    self.template_combo.setCurrentIndex(i)
                    return

    @staticmethod
    def _open_project_dir(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def selected_template_path(self) -> Path | None:
        data = self.template_combo.currentData()
        if data is None:
            return None
        return Path(str(data))

    def set_media_duration_hint(self, seconds: float | None) -> None:
        if seconds is None:
            self.duration_label.setText("Media duration: —")
        else:
            self.duration_label.setText(f"Media duration: {format_timecode(seconds)}")

    def _timing_row(self, field: QLineEdit, nudge_cb) -> QHBoxLayout:  # noqa: ANN001
        row = QHBoxLayout()
        row.addWidget(field, stretch=1)
        for label, delta in (("-5s", -5), ("-1s", -1), ("+1s", 1), ("+5s", 5)):
            btn = QPushButton(label)
            btn.setToolTip(f"Adjust time by {delta:+d} seconds")
            btn.clicked.connect(partial(self._nudge_and_emit, nudge_cb, field, delta))
            self._nudge_buttons.append(btn)
            row.addWidget(btn)
        return row

    def _nudge_and_emit(self, nudge_cb, field: QLineEdit, delta: int) -> None:  # noqa: ANN001
        nudge_cb(field, delta)
        self.timings_changed.emit()

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

    def apply_activity_lock(self, *, downloading: bool, encoding: bool) -> None:
        """Disable fields that must not change while download or FFmpeg work runs."""
        self._lock_downloading = downloading
        self._lock_encoding = encoding

        src_idle = not downloading and not encoding
        self.url_edit.setEnabled(src_idle)
        self.download_btn.setEnabled(not downloading and not encoding)
        self.open_local_btn.setEnabled(src_idle)
        self.cookies_path.setEnabled(src_idle)
        self.cookies_browse.setEnabled(src_idle)
        self.cookies_clear.setEnabled(
            src_idle and bool(self.cookies_path.text().strip())
        )

        timing_idle = not encoding
        self.start_edit.setEnabled(timing_idle)
        self.end_edit.setEnabled(timing_idle)
        for btn in self._nudge_buttons:
            btn.setEnabled(timing_idle)
        self._apply_end_file_btn()

        tpl_idle = not downloading and not encoding
        self.template_combo.setEnabled(tpl_idle)
        self.reload_templates_btn.setEnabled(tpl_idle)

        verse_idle = not encoding
        self.verse_ref.setEnabled(verse_idle)
        self.verse_text.setEnabled(verse_idle)

        audio_idle = not encoding
        self.sermon_vol.setEnabled(audio_idle)
        self.piano_vol.setEnabled(audio_idle)
        self.fade_in.setEnabled(audio_idle)
        self.fade_out.setEnabled(audio_idle)
        self.music_browse.setEnabled(audio_idle)
        self.music_clear_btn.setEnabled(
            audio_idle and bool(self.music_path.text().strip())
        )

        export_idle = not downloading and not encoding
        self.export_btn.setEnabled(export_idle)
        self.save_clip_btn.setEnabled(export_idle)
        self.output_name.setEnabled(export_idle)
        self.open_exports_btn.setEnabled(export_idle)
        self.open_clips_btn.setEnabled(export_idle)
        self.open_downloads_btn.setEnabled(export_idle)

        sub_idle = not downloading and not encoding
        self.subtitle_browse.setEnabled(sub_idle)
        self.subtitle_clear.setEnabled(sub_idle and bool(self.subtitle_path.text().strip()))

        self.cancel_job_btn.setEnabled(downloading or encoding)

    def bind_settings(self, s: QSettings) -> None:
        self._settings = s
        raw = s.value(KEY_LAST_SUBTITLE_FILE, "")
        if raw and Path(str(raw)).is_file():
            self.subtitle_path.setText(str(Path(str(raw)).resolve()))
        raw_m = s.value(KEY_LAST_MUSIC_FILE, "")
        if raw_m and Path(str(raw_m)).is_file():
            self.music_path.setText(str(Path(str(raw_m)).resolve()))
        raw_ck = s.value(KEY_LAST_COOKIES_FILE, "")
        if raw_ck and Path(str(raw_ck)).is_file():
            self.cookies_path.setText(str(Path(str(raw_ck)).resolve()))
        raw_tpl = s.value(KEY_LAST_TEMPLATE_JSON, "")
        if raw_tpl:
            self._apply_saved_template_path(str(raw_tpl))
        raw_ref = s.value(KEY_LAST_VERSE_REF, "")
        if isinstance(raw_ref, str):
            self.verse_ref.setText(raw_ref.strip()[:500])
        raw_body = s.value(KEY_LAST_VERSE_TEXT, "")
        if isinstance(raw_body, str):
            self.verse_text.setPlainText(raw_body[:_MAX_VERSE_BODY_CHARS])

    def _apply_saved_template_path(self, path_str: str) -> None:
        p = Path(path_str)
        if not p.is_file():
            return
        resolved = str(p.resolve())
        self.template_combo.blockSignals(True)
        for i in range(self.template_combo.count()):
            data = self.template_combo.itemData(i)
            if data is not None and str(data) == resolved:
                self.template_combo.setCurrentIndex(i)
                break
        self.template_combo.blockSignals(False)

    def persist_template_and_verse(self) -> None:
        """Write template path and verse fields to QSettings (caller holds QSettings ref)."""
        s = self._settings
        if s is None:
            return
        p = self.selected_template_path()
        if p is not None and p.is_file():
            s.setValue(KEY_LAST_TEMPLATE_JSON, str(p.resolve()))
        else:
            s.remove(KEY_LAST_TEMPLATE_JSON)
        s.setValue(KEY_LAST_VERSE_REF, self.verse_ref.text().strip()[:500])
        s.setValue(KEY_LAST_VERSE_TEXT, self.verse_text.toPlainText()[:_MAX_VERSE_BODY_CHARS])

    def persist_download_prefs(self) -> None:
        """Persist optional yt-dlp cookies file path."""
        s = self._settings
        if s is None:
            return
        text = self.cookies_path.text().strip()
        if text:
            p = Path(text)
            if p.is_file():
                rp = p.resolve()
                s.setValue(KEY_LAST_COOKIES_FILE, str(rp))
                s.setValue(KEY_LAST_COOKIES_DIR, str(rp.parent))
                return
        s.remove(KEY_LAST_COOKIES_FILE)

    def set_end_at_file_end_enabled(self, enabled: bool) -> None:
        self._file_duration_known = enabled
        self._apply_end_file_btn()

    def _apply_end_file_btn(self) -> None:
        self.end_at_file_btn.setEnabled(
            self._file_duration_known
            and not self._lock_downloading
            and not self._lock_encoding
        )

    def _music_start_dir(self) -> str:
        if self._settings is not None:
            raw = self._settings.value(KEY_LAST_MUSIC_DIR, "")
            if raw and Path(str(raw)).is_dir():
                return str(raw)
        return str(Path.home())

    def _subtitle_start_dir(self) -> str:
        if self._settings is not None:
            raw = self._settings.value(KEY_LAST_SUBTITLE_DIR, "")
            if raw and Path(str(raw)).is_dir():
                return str(raw)
        return str(Path.home())

    def _cookies_start_dir(self) -> str:
        if self._settings is not None:
            raw = self._settings.value(KEY_LAST_COOKIES_DIR, "")
            if raw and Path(str(raw)).is_dir():
                return str(raw)
        return str(Path.home())

    def _browse_cookies(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Netscape cookies.txt for yt-dlp",
            self._cookies_start_dir(),
            "Cookies (*.txt);;All files (*)",
        )
        if path:
            self.cookies_path.setText(path)
            if self._settings is not None:
                self._settings.setValue(KEY_LAST_COOKIES_DIR, str(Path(path).parent.resolve()))
                self._settings.setValue(KEY_LAST_COOKIES_FILE, str(Path(path).resolve()))
            self.cookies_clear.setEnabled(
                not self._lock_downloading and not self._lock_encoding
            )

    def _clear_cookies(self) -> None:
        self.cookies_path.clear()
        self.cookies_clear.setEnabled(False)
        if self._settings is not None:
            self._settings.remove(KEY_LAST_COOKIES_FILE)

    def _browse_subtitle(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose subtitles",
            self._subtitle_start_dir(),
            "SubRip (*.srt);;WebVTT (*.vtt);;ASS/SSA (*.ass *.ssa);;All files (*)",
        )
        if path:
            self.subtitle_path.setText(path)
            if self._settings is not None:
                self._settings.setValue(KEY_LAST_SUBTITLE_DIR, str(Path(path).parent.resolve()))
                self._settings.setValue(KEY_LAST_SUBTITLE_FILE, str(Path(path).resolve()))
            self.subtitle_clear.setEnabled(
                not self._lock_downloading and not self._lock_encoding
            )

    def _clear_subtitle(self) -> None:
        self.subtitle_path.clear()
        self.subtitle_clear.setEnabled(False)
        if self._settings is not None:
            self._settings.remove(KEY_LAST_SUBTITLE_FILE)

    def _browse_music(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose piano / instrumental track",
            self._music_start_dir(),
            "Audio (*.mp3 *.wav *.m4a);;All files (*)",
        )
        if path:
            self.music_path.setText(path)
            if self._settings is not None:
                self._settings.setValue(KEY_LAST_MUSIC_DIR, str(Path(path).parent.resolve()))
                self._settings.setValue(KEY_LAST_MUSIC_FILE, str(Path(path).resolve()))
            self.music_clear_btn.setEnabled(
                not self._lock_downloading and not self._lock_encoding
            )

    def _clear_music(self) -> None:
        self.music_path.clear()
        self.music_clear_btn.setEnabled(False)
        if self._settings is not None:
            self._settings.remove(KEY_LAST_MUSIC_FILE)

    def facebook_url(self) -> str:
        return self.url_edit.text().strip()

    def ytdlp_cookies_file(self) -> Path | None:
        text = self.cookies_path.text().strip()
        if not text:
            return None
        p = Path(text)
        return p if p.is_file() else None

    def set_subtitle_path(self, path: Path) -> None:
        self.subtitle_path.setText(str(path.resolve()))
        self.subtitle_clear.setEnabled(True)
        if self._settings is not None:
            self._settings.setValue(KEY_LAST_SUBTITLE_DIR, str(path.parent.resolve()))
            self._settings.setValue(KEY_LAST_SUBTITLE_FILE, str(path.resolve()))

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

    def subtitle_srt_file(self) -> Path | None:
        text = self.subtitle_path.text().strip()
        return Path(text) if text else None

    def output_stem(self) -> str:
        stem = self.output_name.text().strip() or "wordly-export"
        for ch in '<>:"/\\|?*':
            stem = stem.replace(ch, "_")
        return stem

    @staticmethod
    def sanitize_stem_segment(stem: str) -> str:
        """Sanitize a filename stem for the export name field (no extension)."""
        t = stem.strip() or "reel-export"
        for ch in '<>:"/\\|?*':
            t = t.replace(ch, "_")
        t = t.strip("._ ")[:120] or "reel-export"
        return t
