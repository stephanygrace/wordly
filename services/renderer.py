from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from services.audio_mixer import (
    AudioMixSpec,
    build_amix_filter,
    build_piano_filter,
    piano_volume_from_ui,
    sermon_volume_from_ui,
)
from utils.ffmpeg_progress import parse_ffmpeg_progress_seconds
from utils.layout_template import ReelLayoutTemplate, default_layout
from utils.paths import ASSETS, TEMP
from utils.srt_clip import shift_srt_for_trim
from utils.vtt_to_srt import vtt_file_to_srt_file

ProgressCallback = Callable[[float, str], None]
CancelCheck = Callable[[], bool]


def _escape_filter_path(path: Path) -> str:
    s = path.as_posix()
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _find_font_file() -> Optional[str]:
    fonts = ASSETS / "fonts"
    if not fonts.is_dir():
        return None
    for ext in (".ttf", ".otf"):
        for f in sorted(fonts.glob(f"*{ext}")):
            return str(f.resolve())
    return None


def build_filter_complex(
    *,
    start_s: float,
    end_s: float,
    duration_s: float,
    verse_ref_path: Path,
    verse_body_path: Path,
    sermon_volume_pct: int,
    piano_volume_pct: int,
    piano_fade_in: bool,
    piano_fade_out: bool,
    fontfile: Optional[str],
    layout: ReelLayoutTemplate,
    has_sermon_audio: bool,
    clip_subtitle_srt: Optional[Path] = None,
) -> tuple[str, str]:
    spec = AudioMixSpec(
        sermon_volume=sermon_volume_from_ui(sermon_volume_pct),
        piano_volume=piano_volume_from_ui(piano_volume_pct),
        piano_fade_in=piano_fade_in,
        piano_fade_out=piano_fade_out,
        clip_duration_seconds=duration_s,
    )

    ref_esc = _escape_filter_path(verse_ref_path)
    body_esc = _escape_filter_path(verse_body_path)

    font_clause = ""
    if fontfile:
        font_clause = f":fontfile={_escape_filter_path(Path(fontfile))}"

    top = layout.top_overlay_px
    mid_h = layout.middle_video_px
    w = layout.width
    h = layout.height
    fps = layout.fps
    alpha = layout.overlay_background_alpha

    ref_fs = max(22, int(34 * top / 480))
    body_fs = max(24, int(38 * top / 480))
    y_ref = max(20, int(56 * top / 480))
    y_body = max(72, int(130 * top / 480))

    piano = build_piano_filter(spec)
    mix = build_amix_filter()

    vid_chain = (
        f"[0:v]trim=start={start_s:.6f}:duration={duration_s:.6f},setpts=PTS-STARTPTS,"
        f"scale={w}:{mid_h}:force_original_aspect_ratio=increase,crop={w}:{mid_h},setpts=PTS-STARTPTS[vid]"
    )

    if has_sermon_audio:
        audio_chain = (
            f"[0:a]atrim=start={start_s:.6f}:duration={duration_s:.6f},asetpts=PTS-STARTPTS[sa]"
        )
    else:
        audio_chain = (
            f"anullsrc=channel_layout=stereo:sample_rate=48000,"
            f"atrim=start=0:duration={duration_s:.6f},asetpts=PTS-STARTPTS[sa]"
        )

    draw = (
        f"[lay]drawbox=x=0:y=0:w=iw:h={top}:color=black@{alpha:.3f}:t=fill[strip];"
        f"[strip]drawtext=textfile='{ref_esc}'{font_clause}:fontsize={ref_fs}:fontcolor=white:"
        f"x=(w-text_w)/2:y={y_ref}[vref];"
        f"[vref]drawtext=textfile='{body_esc}'{font_clause}:fontsize={body_fs}:fontcolor=white:"
        f"x=(w-text_w)/2:y={y_body}:line_spacing=18[vout]"
    )

    fonts_dir = ASSETS / "fonts"
    sub_tail = ""
    video_out = "vout"
    if clip_subtitle_srt is not None:
        sub_esc = _escape_filter_path(clip_subtitle_srt)
        fs = max(20, min(44, layout.bottom_reserved_px // 17))
        margin_v = max(16, min(120, layout.bottom_reserved_px // 8))
        fonts_clause = ""
        if fonts_dir.is_dir():
            fonts_clause = f":fontsdir='{_escape_filter_path(fonts_dir)}'"
        suf = clip_subtitle_srt.suffix.lower()
        if suf in (".ass", ".ssa"):
            # Preserve embedded styles / karaoke tags; avoid force_style overrides.
            sub_tail = f";[vout]subtitles='{sub_esc}'{fonts_clause}[vfinal]"
        else:
            fstyle = f"Alignment=2\\,MarginV={margin_v}\\,Outline=2\\,FontSize={fs}"
            sub_tail = f";[vout]subtitles='{sub_esc}'{fonts_clause}:force_style={fstyle}[vfinal]"
        video_out = "vfinal"

    segments = [
        vid_chain,
        audio_chain,
        f"color=c=0x141418:s={w}x{h}:d={duration_s:.6f}:r={fps}[base]",
        f"[base][vid]overlay=0:{layout.video_overlay_y}:shortest=1[lay]",
        draw + sub_tail,
        f"[sa]volume={spec.sermon_volume:.5f}[sermon]",
        piano,
        mix,
    ]
    return ";".join(segments), video_out


def render_vertical_reel(
    *,
    sermon_path: Path,
    piano_path: Path,
    output_path: Path,
    start_s: float,
    end_s: float,
    verse_reference: str,
    verse_text: str,
    sermon_volume_pct: int,
    piano_volume_pct: int,
    piano_fade_in: bool,
    piano_fade_out: bool,
    has_sermon_audio: bool = True,
    layout: Optional[ReelLayoutTemplate] = None,
    srt_path: Optional[Path] = None,
    progress_cb: Optional[ProgressCallback] = None,
    should_cancel: Optional[CancelCheck] = None,
) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH. Install FFmpeg and retry.")

    lay = layout or default_layout()
    duration_s = max(0.01, end_s - start_s)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    TEMP.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix="_ref.txt",
        dir=TEMP,
        delete=False,
    ) as ref_f:
        ref_f.write(verse_reference.strip() or " ")
        ref_path = Path(ref_f.name)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix="_body.txt",
        dir=TEMP,
        delete=False,
    ) as body_f:
        body_f.write((verse_text.strip() or " ") + "\n")
        body_path = Path(body_f.name)

    font = _find_font_file()
    clip_subtitle_track: Optional[Path] = None
    vtt_bridge_srt: Optional[Path] = None

    if srt_path is not None and srt_path.is_file():
        suf = srt_path.suffix.lower()

        if suf in (".ass", ".ssa"):
            shifted = Path(
                tempfile.NamedTemporaryFile(
                    suffix="_clip.ass",
                    dir=TEMP,
                    delete=False,
                ).name
            )
            try:
                from utils.ass_clip import shift_ass_for_trim

                n = shift_ass_for_trim(
                    source_ass=srt_path,
                    clip_start_s=start_s,
                    clip_end_s=end_s,
                    dest_ass=shifted,
                )
            except (OSError, ValueError, UnicodeError, RuntimeError) as exc:
                shifted.unlink(missing_ok=True)
                raise RuntimeError(f"Could not read ASS/SSA subtitles: {exc}") from exc
            if n > 0:
                clip_subtitle_track = shifted
            else:
                shifted.unlink(missing_ok=True)
        else:
            source_for_shift: Optional[Path] = srt_path
            if suf == ".vtt":
                vtt_bridge_srt = Path(
                    tempfile.NamedTemporaryFile(
                        suffix="_fromvtt.srt",
                        dir=TEMP,
                        delete=False,
                    ).name
                )
                try:
                    n_vtt = vtt_file_to_srt_file(srt_path, vtt_bridge_srt)
                except (OSError, ValueError, UnicodeError) as exc:
                    vtt_bridge_srt.unlink(missing_ok=True)
                    vtt_bridge_srt = None
                    raise RuntimeError(f"Could not read WebVTT subtitles: {exc}") from exc
                if n_vtt == 0:
                    vtt_bridge_srt.unlink(missing_ok=True)
                    vtt_bridge_srt = None
                    source_for_shift = None
                else:
                    source_for_shift = vtt_bridge_srt

            if source_for_shift is not None:
                shifted = Path(
                    tempfile.NamedTemporaryFile(
                        suffix="_clip.srt",
                        dir=TEMP,
                        delete=False,
                    ).name
                )
                try:
                    n = shift_srt_for_trim(
                        source_srt=source_for_shift,
                        clip_start_s=start_s,
                        clip_end_s=end_s,
                        dest_srt=shifted,
                    )
                except (OSError, ValueError, UnicodeError) as exc:
                    shifted.unlink(missing_ok=True)
                    raise RuntimeError(f"Could not read subtitles file: {exc}") from exc
                finally:
                    if vtt_bridge_srt is not None:
                        vtt_bridge_srt.unlink(missing_ok=True)
                        vtt_bridge_srt = None

                if n > 0:
                    clip_subtitle_track = shifted
                else:
                    shifted.unlink(missing_ok=True)

    fc, video_map = build_filter_complex(
        start_s=start_s,
        end_s=end_s,
        duration_s=duration_s,
        verse_ref_path=ref_path,
        verse_body_path=body_path,
        sermon_volume_pct=sermon_volume_pct,
        piano_volume_pct=piano_volume_pct,
        piano_fade_in=piano_fade_in,
        piano_fade_out=piano_fade_out,
        fontfile=font,
        layout=lay,
        has_sermon_audio=has_sermon_audio,
        clip_subtitle_srt=clip_subtitle_track,
    )

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(sermon_path),
        "-i",
        str(piano_path),
        "-filter_complex",
        fc,
        "-map",
        f"[{video_map}]",
        "-map",
        "[aout]",
        "-r",
        str(lay.fps),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(lay.video_crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert proc.stderr is not None
    stderr_tail: deque[str] = deque(maxlen=40)

    def reader() -> None:
        try:
            for line in proc.stderr:
                stderr_tail.append(line.rstrip()[:500])
                if progress_cb is None:
                    continue
                t = parse_ffmpeg_progress_seconds(line)
                if t is None:
                    continue
                ratio = max(0.0, min(1.0, t / duration_s))
                progress_cb(ratio, "Rendering…")
        except OSError:
            pass

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    try:
        while True:
            if should_cancel and should_cancel():
                proc.kill()
                proc.wait(timeout=30)
                raise RuntimeError("Cancelled")
            if proc.poll() is not None:
                break
            time.sleep(0.15)
        proc.wait(timeout=30)
    finally:
        th.join(timeout=5.0)
        try:
            ref_path.unlink(missing_ok=True)
            body_path.unlink(missing_ok=True)
            if clip_subtitle_track is not None:
                clip_subtitle_track.unlink(missing_ok=True)
        except OSError:
            pass

    if proc.returncode != 0:
        tail = "\n".join(stderr_tail).strip()
        msg = f"ffmpeg exited with code {proc.returncode}"
        if tail:
            msg += "\n\nLast FFmpeg log lines:\n" + tail
        raise RuntimeError(msg)

    if progress_cb:
        progress_cb(1.0, "Done")
    return output_path.resolve()
