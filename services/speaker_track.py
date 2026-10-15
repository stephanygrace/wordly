"""Find and follow the speaker in a clip (Apple Vision person + face detection).

Used for the reel: the blurred background is a portrait window framing the
speaker head to feet, and the main 16:9 video is zoomed in head to waist. Both
use a "lazy camera" that holds still and only pans when the speaker nears the
edge of the frame, so the shot stays steady instead of chasing them. Frames are sampled a few times per
second, people are linked into tracks across samples, one track is chosen
(automatically, or the person nearest a point the user clicked), and its
position is smoothed into a camera path.
"""

from __future__ import annotations

import math
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

SAMPLE_FPS = 4.0
SAMPLE_WIDTH = 640
# Link a detection to a track when its centre moved less than this (fraction of width).
MATCH_DISTANCE = 0.12
SMOOTH_WINDOW_S = 1.2
# Zoom changes more slowly than panning so raised arms don't make the frame "breathe".
ZOOM_SMOOTH_WINDOW_S = 3.0
# Space above the head and below the feet, as a fraction of the body's height.
BODY_MARGIN = 0.08
# Head-to-waist framing for the main video: space above the head / below the waist.
UPPER_MARGIN = 0.10
# Lazy camera: the frame holds still while the speaker stays within this distance of
# the frame centre (fraction of the crop width); beyond it, it pans just enough.
DEAD_ZONE = 0.22
# How long a pan takes to settle (seconds).
GLIDE_S = 1.6


@dataclass(frozen=True)
class Box:
    """Normalised box, top-left origin (x, y = top-left corner)."""

    x: float
    y: float
    w: float
    h: float
    has_face: bool = False
    # Horizontal centre of the face when one was found (gestures widen the body box).
    face_cx: float | None = None
    # Head-to-waist box (Vision upper-body detection), top and height.
    upper_y: float | None = None
    upper_h: float | None = None

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def focus_x(self) -> float:
        return self.face_cx if self.face_cx is not None else self.cx

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def waist_h(self) -> float:
        """Head-to-waist height; estimated as half the body when not detected."""
        return self.upper_h if self.upper_h is not None else self.h * 0.5

    @property
    def waist_cy(self) -> float:
        top = self.upper_y if self.upper_y is not None else self.y
        return top + self.waist_h / 2

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h


@dataclass
class Track:
    boxes: dict[int, Box] = field(default_factory=dict)  # sample index → box

    def last(self) -> tuple[int, Box]:
        i = max(self.boxes)
        return i, self.boxes[i]

    def score(self, samples: int) -> float:
        """Prominence: present, big, facing the camera, near the middle of the frame."""
        if not self.boxes:
            return 0.0
        total = 0.0
        for box in self.boxes.values():
            centrality = 1.0 - min(1.0, abs(box.cx - 0.5) * 1.2)
            total += box.h * (1.5 if box.has_face else 1.0) * (0.4 + 0.6 * centrality)
        return total / max(1, samples)


def vision_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Vision  # noqa: F401
    except ImportError:
        return False
    return True


def _detect(image: Path) -> list[Box]:
    import Vision
    from Foundation import NSURL

    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(image)), {})
    people = Vision.VNDetectHumanRectanglesRequest.alloc().init()
    people.setUpperBodyOnly_(False)
    uppers = Vision.VNDetectHumanRectanglesRequest.alloc().init()
    uppers.setUpperBodyOnly_(True)
    faces = Vision.VNDetectFaceRectanglesRequest.alloc().init()
    handler.performRequests_error_([people, uppers, faces], None)

    def top_left(r) -> tuple[float, float, float, float]:
        b = r.boundingBox()  # Vision: bottom-left origin
        return b.origin.x, 1.0 - b.origin.y - b.size.height, b.size.width, b.size.height

    face_centres = []
    for r in faces.results() or []:
        x, y, w, h = top_left(r)
        face_centres.append((x + w / 2, y + h / 2))
    upper_boxes = [top_left(r) for r in uppers.results() or []]
    boxes = []
    for r in people.results() or []:
        x, y, w, h = top_left(r)
        # A face in the top part of the body box means this person faces the camera.
        face = next((fx for fx, fy in face_centres if x <= fx <= x + w and y <= fy <= y + h * 0.45), None)
        upper = _matching_upper(x, y, w, h, upper_boxes)
        boxes.append(
            Box(x, y, w, h, face is not None, face, upper[1] if upper else None, upper[3] if upper else None)
        )
    return boxes


def _matching_upper(
    x: float, y: float, w: float, h: float, uppers: list[tuple[float, float, float, float]]
) -> tuple[float, float, float, float] | None:
    """The upper-body box belonging to a full-body box: centred inside it, top near its top."""
    best, best_d = None, 0.12
    for ux, uy, uw, uh in uppers:
        ucx = ux + uw / 2
        if not (x <= ucx <= x + w) or uh > h * 1.05:
            continue
        d = abs(uy - y) + abs(ucx - (x + w / 2)) * 0.5
        if d < best_d:
            best, best_d = (ux, uy, uw, uh), d
    return best


def sample_detections(
    src: Path,
    *,
    fps: float = SAMPLE_FPS,
    detector: Callable[[Path], list[Box]] = _detect,
) -> list[list[Box]]:
    """People found in frames sampled every 1/fps seconds."""
    with tempfile.TemporaryDirectory(prefix="wordly_track_") as tmp:
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-i",
                str(src),
                "-vf",
                f"fps={fps:g},scale={SAMPLE_WIDTH}:-2",
                "-q:v",
                "4",
                str(Path(tmp) / "f_%05d.jpg"),
            ],
            check=True,
            capture_output=True,
        )
        return [detector(frame) for frame in sorted(Path(tmp).glob("f_*.jpg"))]


def build_tracks(samples: list[list[Box]]) -> list[Track]:
    """Greedily link detections across samples by centre distance."""
    tracks: list[Track] = []
    for i, boxes in enumerate(samples):
        free = list(range(len(tracks)))
        for box in sorted(boxes, key=lambda b: -b.h):
            best, best_d = None, MATCH_DISTANCE
            for t in free:
                last_i, last_box = tracks[t].last()
                if i - last_i > SAMPLE_FPS * 3:  # lost for > 3 s
                    continue
                d = math.hypot(box.cx - last_box.cx, (box.cy - last_box.cy) * 0.5)
                if d < best_d:
                    best, best_d = t, d
            if best is None:
                tracks.append(Track({i: box}))
            else:
                tracks[best].boxes[i] = box
                free.remove(best)
    return tracks


def choose_track(tracks: list[Track], samples: int, pick: tuple[float, float, float] | None = None) -> Track | None:
    """The track to follow: the one under the user's pick (t, x, y) or the most prominent."""
    if not tracks:
        return None
    if pick is not None:
        t, px, py = pick
        index = round(t * SAMPLE_FPS)
        best, best_d = None, float("inf")
        for track in tracks:
            if not track.boxes:
                continue
            nearest = min(track.boxes, key=lambda i: abs(i - index))
            box = track.boxes[nearest]
            d = 0.0 if box.contains(px, py) else math.hypot(box.cx - px, box.cy - py)
            d += abs(nearest - index) / SAMPLE_FPS * 0.05  # prefer boxes seen near the picked time
            if d < best_d:
                best, best_d = track, d
        return best
    return max(tracks, key=lambda tr: tr.score(samples))


def camera_path(track: Track | None, samples: int) -> list[tuple[float, float, float, float, float]]:
    """Per sample for the followed person: (x, body_cy, body_h, waist_cy, waist_h).

    x is the face (or body) centre; body_* is head to feet, waist_* head to waist.
    Gaps are held/interpolated, then smoothed (sizes more slowly than positions).
    Without a track the path stays on the frame centre.
    """
    if track is None or not track.boxes or samples == 0:
        return [(0.5, 0.5, 1.0, 0.5, 1.0)] * samples

    def values(b: Box) -> tuple[float, ...]:
        return (b.focus_x, b.cy, b.h, b.waist_cy, b.waist_h)

    known = sorted(track.boxes)
    raw: list[tuple[float, ...]] = []
    for i in range(samples):
        if i <= known[0]:
            raw.append(values(track.boxes[known[0]]))
        elif i >= known[-1]:
            raw.append(values(track.boxes[known[-1]]))
        elif i in track.boxes:
            raw.append(values(track.boxes[i]))
        else:
            lo = max(k for k in known if k < i)
            hi = min(k for k in known if k > i)
            a, b = values(track.boxes[lo]), values(track.boxes[hi])
            f = (i - lo) / (hi - lo)
            raw.append(tuple(va + (vb - va) * f for va, vb in zip(a, b)))

    def smoothed(k: int, window_s: float) -> list[float]:
        half = max(1, round(window_s * SAMPLE_FPS / 2))
        out = []
        for i in range(samples):
            window = raw[max(0, i - half) : i + half + 1]
            out.append(sum(v[k] for v in window) / len(window))
        return out

    return list(
        zip(
            smoothed(0, SMOOTH_WINDOW_S),
            smoothed(1, SMOOTH_WINDOW_S),
            smoothed(2, ZOOM_SMOOTH_WINDOW_S),
            smoothed(3, SMOOTH_WINDOW_S),
            smoothed(4, ZOOM_SMOOTH_WINDOW_S),
        )
    )


def analyze(src: Path, pick: tuple[float, float, float] | None = None) -> list[tuple[float, ...]]:
    """Camera path for the speaker in *src* (one detection pass, shared by both crops)."""
    samples = sample_detections(src)
    track = choose_track(build_tracks(samples), len(samples), pick)
    return camera_path(track, len(samples))


def portrait_crop(
    cx: float,
    cy: float,
    src_w: int,
    src_h: int,
    body_h: float = 1.0,
    *,
    aspect: float = 9 / 16,
    min_height: float = 0.25,
) -> tuple[int, int, int, int]:
    """Portrait crop (w, h, x, y) in source pixels framing a whole person.

    The window is the person's height (head to feet, *body_h* as a fraction of
    the frame) plus a small margin, centred on them. When they are too close to
    a side edge for that, it zooms in further so they stay exactly centred.
    """
    want_h = min(src_h, max(src_h * min_height, body_h * src_h * (1 + 2 * BODY_MARGIN)))
    px, py = cx * src_w, cy * src_h
    room = 2 * min(px, src_w - px)
    w = max(want_h * aspect * 0.5, min(want_h * aspect, room))
    h = w / aspect
    x = min(max(px - w / 2, 0), src_w - w)
    y = min(max(py - h / 2, 0), src_h - h)
    # Even sizes for the encoder.
    return int(w) // 2 * 2, int(h) // 2 * 2, int(x), int(y)


def lazy_follow(
    subject: list[float],
    window: float,
    *,
    lo: float,
    hi: float,
    dead_zone: float = DEAD_ZONE,
    glide_s: float = GLIDE_S,
) -> list[float]:
    """Frame centre per sample that only moves when the subject nears the frame edge.

    Like a camera operator: start where the subject usually is, hold still while
    they stay within ``dead_zone * window`` of the centre, otherwise move just far
    enough to bring them back inside, and glide between positions. Centres are
    kept within [lo, hi] so the window never leaves the source frame.
    """
    if not subject:
        return []
    zone = dead_zone * window
    centre = min(max(sorted(subject)[len(subject) // 2], lo), hi)
    held = []
    for s in subject:
        if s > centre + zone:
            centre = s - zone
        elif s < centre - zone:
            centre = s + zone
        centre = min(max(centre, lo), hi)
        held.append(centre)
    half = max(1, round(glide_s * SAMPLE_FPS / 2))
    glided = []
    for i in range(len(held)):
        window_vals = held[max(0, i - half) : i + half + 1]
        glided.append(min(max(sum(window_vals) / len(window_vals), lo), hi))
    return glided


def _at(values: list[float], t: float) -> float:
    pos = t * SAMPLE_FPS
    i = min(int(pos), len(values) - 1)
    j = min(i + 1, len(values) - 1)
    f = pos - int(pos) if j != i else 0.0
    return values[i] + (values[j] - values[i]) * f


def _median(values: list[float], default: float) -> float:
    return sorted(values)[len(values) // 2] if values else default


def crop_rects(
    camera: list[tuple[float, ...]],
    *,
    src_w: int,
    src_h: int,
    frames: int,
    out_fps: float = 30.0,
) -> list[tuple[int, int, int, int]]:
    """Background portrait crop (w, h, x, y) per output frame: whole body, lazy camera.

    Size and height are fixed for the clip (typical head-to-feet height); the
    window pans sideways only when the speaker nears its edge.
    """
    if not camera:
        camera = [(0.5, 0.5, 1.0)]
    body_h = _median([p[2] for p in camera], 1.0)
    body_cy = _median([p[1] for p in camera], 0.5)
    w, h, _, y = portrait_crop(0.5, body_cy, src_w, src_h, body_h)
    xs = lazy_follow([p[0] * src_w for p in camera], w, lo=w / 2, hi=src_w - w / 2)
    return [
        (w, h, int(min(max(_at(xs, n / out_fps) - w / 2, 0), src_w - w)), y)
        for n in range(max(1, frames))
    ]


def main_crop_size(
    camera: list[tuple[float, ...]],
    src_w: int,
    src_h: int,
    *,
    aspect: float = 16 / 9,
    max_zoom: float = 3.0,
) -> tuple[int, int]:
    """16:9 crop (w, h) framing the speaker head to waist, fixed for the whole clip.

    Uses the typical (median) head-to-waist height so the zoom doesn't drift;
    *max_zoom* limits how far a far-away speaker is enlarged (sharpness).
    """
    heights = sorted(p[4] for p in camera) if camera else [1.0]
    waist_h = heights[len(heights) // 2]
    h = min(src_h, max(src_h / max_zoom, waist_h * src_h * (1 + 2 * UPPER_MARGIN)))
    w = min(src_w, h * aspect)
    h = w / aspect
    return int(w) // 2 * 2, int(h) // 2 * 2


def main_crop_positions(
    camera: list[tuple[float, ...]],
    *,
    size: tuple[int, int],
    src_w: int,
    src_h: int,
    frames: int,
    out_fps: float = 30.0,
) -> list[tuple[int, int]]:
    """Top-left (x, y) per output frame for the head-to-waist shot, lazy camera.

    The height is fixed for the clip; the frame pans sideways only when the
    speaker nears its edge, so it stays steady instead of chasing them.
    """
    w, h = size
    if not camera:
        camera = [(0.5, 0.5, 1.0, 0.5, 1.0)]
    waist_cy = _median([p[3] for p in camera], 0.5)
    y = int(min(max(waist_cy * src_h - h / 2, 0), src_h - h))
    xs = lazy_follow([p[0] * src_w for p in camera], w, lo=w / 2, hi=src_w - w / 2)
    return [(int(min(max(_at(xs, n / out_fps) - w / 2, 0), src_w - w)), y) for n in range(max(1, frames))]
