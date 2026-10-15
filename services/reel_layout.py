"""Reel layout for the Resolve export: blurred background, centred video, verse block.

Matches the sermon reel template (1080x1920): the 16:9 sermon video sits full
width in the middle of the frame, zoomed in on the speaker head to waist; a
blurred and darkened portrait crop of the same clip framing them head to feet
fills the background (both keep the speaker centred the whole time); the Bible
verse is centred above the video and captions run below it.

Resolve Free can't blur or composite reliably through an imported FCPXML, so
the background and main videos are pre-rendered with ffmpeg. The verse block
and captions are editable Resolve text titles.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from services.resolve_canvas import DEFAULT_FPS, DEFAULT_HEIGHT, DEFAULT_WIDTH


# Resolve title "Size" → on-screen pixels at 1080x1920, measured against the
# calibrated captions (Outfit Bold size 18 ≈ 32px text).
RESOLVE_PX_PER_SIZE = 1.792
# Resolve's own line spacing in a multi-line title, relative to the text size.
RESOLVE_LINE_PITCH = 46 / 36


@dataclass(frozen=True)
class ReelLayout:
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    # Background: blur strength (gaussian sigma at 1/4 scale) and brightness multiplier.
    background_blur: float = 4.0
    background_brightness: float = 0.22
    # Verse block above the video.
    verse_font: str = "Outfit"  # bundled in assets/fonts; matches the template
    verse_max_width_px: int = 740  # the template's ~700px wrap, scaled to this text size
    verse_gap_px: int = 50  # between the verse block and the top of the video
    verse_color: str = "#FFFFFF"
    # Verse text: Outfit SemiBold at this Resolve title size (the Inspector "Size" value).
    verse_resolve_size: int = 20
    # Main video: how far a far-away speaker may be enlarged (sharpness limit).
    main_max_zoom: float = 3.0

    @property
    def verse_font_px(self) -> int:
        """On-screen text size of the verse: Resolve size × ~1.79 px (measured in Resolve)."""
        return round(self.verse_resolve_size * RESOLVE_PX_PER_SIZE)

    @property
    def verse_line_px(self) -> int:
        """Line spacing of a multi-line Resolve title (measured: ~46px for 36px text)."""
        return round(self.verse_font_px * RESOLVE_LINE_PITCH)

    @property
    def video_height(self) -> int:
        """Height of a full-width 16:9 video in the frame (608 at 1080 wide)."""
        return round(self.width * 9 / 16)

    @property
    def video_top(self) -> int:
        return (self.height - self.video_height) // 2

    @property
    def video_bottom(self) -> int:
        return self.video_top + self.video_height


DEFAULT_LAYOUT = ReelLayout()


def background_filter(layout: ReelLayout = DEFAULT_LAYOUT) -> str:
    """ffmpeg filter: cover-fill from the centre, blur, darken, 1080x1920 video.

    Used when the speaker can't be tracked. Blurring at quarter resolution then
    scaling up is ~16x cheaper than blurring a full 1080x1920 frame and looks
    the same once it is this soft.
    """
    qw, qh = layout.width // 4, layout.height // 4
    b = layout.background_brightness
    return (
        f"scale={qw}:{qh}:force_original_aspect_ratio=increase,crop={qw}:{qh},"
        f"gblur=sigma={layout.background_blur:g},"
        f"scale={layout.width}:{layout.height}:flags=bicubic,"
        f"colorchannelmixer=rr={b:g}:gg={b:g}:bb={b:g},"
        f"fps={DEFAULT_FPS:g},format=yuv420p"
    )


def _probe_video(src: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "default=noprint_wrappers=1",
            str(src),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return int(values["width"]), int(values["height"]), float(values["duration"])


# Working size for the speaker-following crop: the background ends up heavily
# blurred, so cropping from a 640x360 decode looks identical and is fast.
_TRACK_DECODE_W, _TRACK_DECODE_H = 640, 360


def render_background(
    src: Path,
    dest: Path,
    layout: ReelLayout = DEFAULT_LAYOUT,
    *,
    pick: tuple[float, float, float] | None = None,
    camera: list[tuple[float, ...]] | None = None,
) -> Path:
    """Render the blurred, darkened portrait background video for one clip.

    A portrait window frames the speaker head to feet and follows them (Apple
    Vision) so they stay centred the whole clip; *pick* = (seconds, x, y) chooses
    whom to follow when several people are on screen, or pass the *camera* path
    from :func:`speaker_track.analyze` to reuse one detection pass. Without
    Vision the frame is simply cover-filled from the centre.

    Frames are cropped here with numpy rather than with ffmpeg's crop commands:
    ffmpeg aborts when a crop's size changes mid-stream, and the size changes
    whenever the zoom follows the speaker.
    """
    from services import speaker_track as st

    dest.parent.mkdir(parents=True, exist_ok=True)
    encode = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", str(dest)]
    if camera is None and not st.vision_available():
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-an", "-vf", background_filter(layout), *encode],
            check=True,
            capture_output=True,
        )
        return dest

    import numpy as np

    _, _, duration = _probe_video(src)
    if camera is None:
        camera = st.analyze(src, pick)
    frames = max(1, round(duration * DEFAULT_FPS))
    rects = st.crop_rects(
        camera,
        src_w=_TRACK_DECODE_W,
        src_h=_TRACK_DECODE_H,
        frames=frames,
        out_fps=DEFAULT_FPS,
    )
    ow, oh = layout.width // 4, layout.height // 4
    b = layout.background_brightness
    finish = (
        f"gblur=sigma={layout.background_blur:g},"
        f"scale={layout.width}:{layout.height}:flags=bicubic,"
        f"colorchannelmixer=rr={b:g}:gg={b:g}:bb={b:g}"
    )
    decoder = subprocess.Popen(
        [
            "ffmpeg", "-loglevel", "error", "-i", str(src), "-an",
            "-vf", f"fps={DEFAULT_FPS:g},scale={_TRACK_DECODE_W}:{_TRACK_DECODE_H}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        stdout=subprocess.PIPE,
    )
    encoder = subprocess.Popen(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{ow}x{oh}", "-r", f"{DEFAULT_FPS:g}", "-i", "-",
            "-vf", finish, *encode,
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert decoder.stdout is not None and encoder.stdin is not None
    frame_bytes = _TRACK_DECODE_W * _TRACK_DECODE_H * 3
    n = 0
    try:
        while True:
            raw = decoder.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(_TRACK_DECODE_H, _TRACK_DECODE_W, 3)
            w, h, x, y = rects[min(n, len(rects) - 1)]
            # Nearest-neighbour resize of the crop to the working size (it is blurred next).
            rows = (y + (np.arange(oh) + 0.5) * h / oh).astype(np.intp).clip(0, _TRACK_DECODE_H - 1)
            cols = (x + (np.arange(ow) + 0.5) * w / ow).astype(np.intp).clip(0, _TRACK_DECODE_W - 1)
            encoder.stdin.write(np.ascontiguousarray(frame[rows][:, cols]).tobytes())
            n += 1
    finally:
        encoder.stdin.close()
        decoder.stdout.close()
        decoder.wait()
        err = encoder.stderr.read().decode(errors="replace") if encoder.stderr else ""
        encoder.wait()
    if encoder.returncode != 0 or n == 0:
        raise RuntimeError(f"Background render failed for {src.name}: {err.strip() or 'no frames decoded'}")
    return dest


def camera_shake(transforms: str) -> float:
    """Typical frame-to-frame camera movement in pixels, from a vid.stab ASCII file.

    Uses the median of each frame's local motions (robust to people moving),
    then the 90th percentile over frames. ~0 for a tripod, several px handheld.
    """
    import re
    import statistics

    per_frame = []
    for frame in transforms.split("\nFrame ")[1:]:
        motions = [(int(a), int(b)) for a, b in re.findall(r"\(LM (-?\d+) (-?\d+) ", frame)]
        if len(motions) >= 10:
            dx = statistics.median(a for a, _ in motions)
            dy = statistics.median(b for _, b in motions)
            per_frame.append(abs(dx) + abs(dy))
    if not per_frame:
        return 0.0
    return sorted(per_frame)[int(len(per_frame) * 0.9)]


# Stabilise only when the camera itself moves this much (px per frame, 90th pct).
SHAKE_THRESHOLD_PX = 1.0


def stabilize_if_shaky(src: Path, dest: Path) -> Path:
    """Return a stabilised copy of *src* when the camera shakes, else *src* unchanged.

    Two-pass vid.stab: measure (fast), then smooth the camera path and zoom in
    slightly to hide the moving borders. Tripod footage skips the second pass
    and keeps its original quality.
    """
    with tempfile.TemporaryDirectory(prefix="wordly_stab_") as tmp:
        transforms = Path(tmp) / "transforms.trf"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                # ASCII: some builds can't read back their own binary transform files.
                "-vf", f"vidstabdetect=shakiness=5:accuracy=9:fileformat=ascii:result='{_filter_path(transforms)}'",
                "-f", "null", "-",
            ],
            check=True,
            capture_output=True,
        )
        text = transforms.read_text(encoding="ascii", errors="replace")
        if camera_shake(text) < SHAKE_THRESHOLD_PX:
            return src
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                "-vf", f"vidstabtransform=input='{_filter_path(transforms)}':smoothing=30:optzoom=1:interpol=bicubic",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                str(dest),
            ],
            check=True,
            capture_output=True,
        )
    return dest


def background_join_filter(durations_s: list[float], handle_s: float) -> str:
    """ffmpeg filtergraph: the per-clip backgrounds as one track, cross-fading at each cut.

    Each input carries *handle_s* of hidden media at both ends. The fades are
    2 x handle_s long and centred on the cuts, matching the main clips' dissolves,
    so the output is exactly as long as the timeline.
    """
    n = len(durations_s)
    parts = []
    for i, dur in enumerate(durations_s):
        start = handle_s if i == 0 else 0.0
        end = dur - handle_s if i == n - 1 else dur
        # xfade needs one constant frame rate on both sides: the timeline's.
        parts.append(f"[{i}:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,fps={DEFAULT_FPS:g}[v{i}]")
    last, cut = "v0", 0.0
    for i in range(1, n):
        cut += durations_s[i - 1] - 2 * handle_s
        out = f"x{i}"
        parts.append(
            f"[{last}][v{i}]xfade=transition=fade:duration={2 * handle_s:.3f}:offset={cut - handle_s:.3f}[{out}]"
        )
        last = out
    parts.append(f"[{last}]format=yuv420p[out]")
    return ";".join(parts)


def join_backgrounds(backgrounds: list[Path], dest: Path, *, durations_s: list[float], handle_s: float) -> Path:
    """One continuous background video for the timeline, fading between clips."""
    inputs = [arg for bg in backgrounds for arg in ("-i", str(bg))]
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", *inputs,
            "-filter_complex", background_join_filter(durations_s, handle_s), "-map", "[out]", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def render_main(
    src: Path,
    dest: Path,
    camera: list[tuple[float, ...]],
    layout: ReelLayout = DEFAULT_LAYOUT,
) -> Path:
    """Render the main 16:9 video zoomed in on the speaker head to waist (audio kept).

    The zoom is fixed for the clip and only the position follows the speaker,
    which ffmpeg's crop can change per frame (a size change would abort it).
    """
    from services import speaker_track as st

    dest.parent.mkdir(parents=True, exist_ok=True)
    width, height, duration = _probe_video(src)
    size = st.main_crop_size(camera, width, height, max_zoom=layout.main_max_zoom)
    frames = max(1, round(duration * DEFAULT_FPS))
    positions = st.main_crop_positions(camera, size=size, src_w=width, src_h=height, frames=frames, out_fps=DEFAULT_FPS)
    w, h = size
    with tempfile.TemporaryDirectory(prefix="wordly_main_") as tmp:
        commands = Path(tmp) / "crop.cmd"
        commands.write_text(
            "\n".join(f"{n / DEFAULT_FPS:.4f} crop x {x}, crop y {y};" for n, (x, y) in enumerate(positions)) + "\n",
            encoding="utf-8",
        )
        x0, y0 = positions[0]
        out_w, out_h = layout.width, layout.video_height // 2 * 2
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                "-vf",
                f"fps={DEFAULT_FPS:g},sendcmd=f='{_filter_path(commands)}',crop=w={w}:h={h}:x={x0}:y={y0},"
                f"scale={out_w}:{out_h}:flags=lanczos,setsar=1",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                str(dest),
            ],
            check=True,
            capture_output=True,
        )
    return dest


def _filter_path(path: Path) -> str:
    """Escape a path for use inside an ffmpeg filtergraph option."""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


@dataclass(frozen=True)
class VerseLine:
    """One wrapped line of the verse block and where its centre sits on screen."""

    text: str
    bold: bool  # the reference line ("1 Corinthians 2:9")
    position_y: float  # FCPXML percent of frame height from centre


def verse_lines(reference: str, body: str, layout: ReelLayout = DEFAULT_LAYOUT) -> list[VerseLine]:
    """Wrap the verse like the template and place each line above the video.

    The lines go into one editable Resolve title, separated by line breaks.
    Wrapping is measured with the bundled Outfit font (needs a running Qt
    application); the block is bottom-anchored above the video so longer verses
    grow upward.
    """
    from PySide6.QtGui import QFont, QFontMetrics, QGuiApplication

    if QGuiApplication.instance() is None:
        raise RuntimeError("verse_lines needs a running Qt application (font metrics).")
    from utils.fonts import register_bundled_fonts

    register_bundled_fonts()
    font = QFont(layout.verse_font)
    font.setPixelSize(layout.verse_font_px)
    font.setWeight(QFont.Weight.DemiBold)
    metrics = QFontMetrics(font)

    lines: list[tuple[str, bool]] = []
    if reference.strip():
        lines.append((reference.strip(), True))
    for paragraph in body.strip().splitlines():
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if current and metrics.horizontalAdvance(candidate) > layout.verse_max_width_px:
                lines.append((current, False))
                current = word
            else:
                current = candidate
        if current:
            lines.append((current, False))

    top = layout.video_top - layout.verse_gap_px - layout.verse_line_px * len(lines)
    out = []
    for i, (text, bold) in enumerate(lines):
        centre = top + i * layout.verse_line_px + layout.verse_line_px / 2
        out.append(VerseLine(text, bold, round((layout.height / 2 - centre) / layout.height * 100, 2)))
    return out


def caption_position_y(layout: ReelLayout = DEFAULT_LAYOUT, below_video_px: int = 121) -> float:
    """Caption centre as FCPXML percent-from-centre (template: ~121px under the video)."""
    centre = layout.video_bottom + below_video_px
    return round((layout.height / 2 - centre) / layout.height * 100, 1)
