"""Spot when the speaker is reading from the projector screen (Apple Vision face pose).

The screen hangs at the speaker's top right, outside the camera's view (camera
left). To read a slide or a long verse they turn their head toward it, so Vision
either sees the face turned to camera left (negative yaw) or loses it entirely.
Preaching to the room moves the head side to side; reading holds it there, so a
stretch where the face is mostly turned that way is the speaker reading, which
looks odd in a reel because the viewer can't see what they're looking at.

Faces are sampled once a second, in 10-minute chunks that are cached like the
transcript, and the speaker is followed as the face of the usual size and place.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from services.speaker_track import vision_available
from utils.paths import TEMP

ProgressCb = Optional[Callable[[float, str], None]]
ShouldCancel = Optional[Callable[[], bool]]

CHUNK_S = 600
GAZE_CACHE = TEMP / "gaze"
# Faces turned further than this toward camera left (radians) are looking at the screen.
SCREEN_YAW = -0.2
# Reading: turned to the screen (or face lost) for this share of any window this long.
READING_WINDOW_S = 15
READING_SHARE = 0.7
# Following the speaker: same face if it moved less than this (fraction of width)
# and is about the same size; after this many seconds lost, take the nearest such face.
MATCH_DISTANCE = 0.1
SIZE_RATIO = (0.6, 1.6)
REACQUIRE_S = 5


@dataclass(frozen=True)
class Face:
    cx: float
    w: float
    yaw: float


def _cache_key(src: Path) -> str:
    stat = src.stat()
    return hashlib.sha1(f"{src.resolve()}|{stat.st_size}|{int(stat.st_mtime)}".encode()).hexdigest()[:16]


def _detect_faces(image: Path) -> list[Face]:
    import Vision
    from Foundation import NSURL

    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(image)), {})
    request = Vision.VNDetectFaceRectanglesRequest.alloc().init()
    request.setRevision_(3)  # head pose (yaw) on every face
    handler.performRequests_error_([request], None)
    faces = []
    for r in request.results() or []:
        box = r.boundingBox()
        yaw = r.yaw()
        faces.append(Face(box.origin.x + box.size.width / 2, box.size.width, float(yaw) if yaw is not None else 0.0))
    return faces


def sample_faces(
    src: Path,
    ranges: list[tuple[float, float]],
    *,
    progress_cb: ProgressCb = None,
    should_cancel: ShouldCancel = None,
    detector: Callable[[Path], list[Face]] = _detect_faces,
) -> dict[int, list[Face]]:
    """Faces found once a second inside *ranges*, keyed by whole second of the video."""
    needed = sorted({i for a, b in ranges for i in range(int(a // CHUNK_S), max(int(a // CHUNK_S) + 1, math.ceil(b / CHUNK_S)))})
    key = _cache_key(src)
    GAZE_CACHE.mkdir(parents=True, exist_ok=True)
    faces: dict[int, list[Face]] = {}
    for k, i in enumerate(needed):
        if should_cancel is not None and should_cancel():
            raise RuntimeError("Cancelled")
        cache = GAZE_CACHE / f"{key}_{i:03d}.json"
        if not cache.is_file():
            if progress_cb is not None:
                progress_cb(k / len(needed), f"Watching where the speaker looks (part {k + 1} of {len(needed)})…")
            offset = i * CHUNK_S
            with tempfile.TemporaryDirectory(prefix="wordly_gaze_") as tmp:
                subprocess.run(
                    [
                        "ffmpeg", "-loglevel", "error", "-ss", f"{offset}", "-t", f"{CHUNK_S}", "-i", str(src),
                        "-vf", "fps=1", "-q:v", "4", str(Path(tmp) / "f_%04d.jpg"),
                    ],
                    check=True,
                    capture_output=True,
                )
                chunk = {
                    offset + n: [[f.cx, f.w, f.yaw] for f in detector(frame)]
                    for n, frame in enumerate(sorted(Path(tmp).glob("f_*.jpg")))
                }
            cache.write_text(json.dumps(chunk), encoding="utf-8")
        for t, found in json.loads(cache.read_text(encoding="utf-8")).items():
            faces[int(t)] = [Face(*f) for f in found]
    return {t: f for t, f in faces.items() if any(a <= t < b for a, b in ranges)}


def _same_size(face: Face, size: float) -> bool:
    return SIZE_RATIO[0] < face.w / size < SIZE_RATIO[1]


def speaker_yaws(faces: dict[int, list[Face]], seconds: list[int]) -> dict[int, float | None]:
    """The speaker's head yaw each second (None when their face wasn't found)."""
    seen = [f for t in seconds for f in faces.get(t, [])]
    if not seen:
        return {t: None for t in seconds}
    # The speaker is the face most often seen at about the same place and size.
    candidates = seen[:: max(1, len(seen) // 300)]
    anchor = max(
        candidates, key=lambda f: sum(1 for g in seen if abs(g.cx - f.cx) < MATCH_DISTANCE and _same_size(g, f.w))
    )
    pos, size, lost = anchor.cx, anchor.w, 0
    out: dict[int, float | None] = {}
    for t in seconds:
        near = [
            f for f in faces.get(t, [])
            if _same_size(f, size) and (abs(f.cx - pos) < MATCH_DISTANCE or lost >= REACQUIRE_S)
        ]
        if near:
            face = min(near, key=lambda f: abs(f.cx - pos))
            out[t], pos, lost = face.yaw, face.cx, 0
        else:
            out[t] = None
            lost += 1
    return out


def reading_stretches(yaws: dict[int, float | None]) -> list[tuple[float, float]]:
    """Start/end seconds where the speaker keeps facing the screen (merged)."""
    seconds = sorted(yaws)
    toward = {t for t in seconds if yaws[t] is None or yaws[t] < SCREEN_YAW}
    present = set(seconds)
    stretches: list[list[float]] = []
    for t in seconds:
        window = range(t, t + READING_WINDOW_S)
        if not all(s in present for s in window):
            continue
        if sum(s in toward for s in window) >= READING_SHARE * READING_WINDOW_S:
            if stretches and t <= stretches[-1][1]:
                stretches[-1][1] = t + READING_WINDOW_S
            else:
                stretches.append([t, t + READING_WINDOW_S])
    # Trim each stretch to the seconds actually spent facing the screen.
    out = []
    for a, b in stretches:
        inside = [t for t in seconds if a <= t < b and t in toward]
        out.append((inside[0], inside[-1] + 1))
    return out


def find_screen_reading(
    src: Path,
    ranges: list[tuple[float, float]],
    *,
    progress_cb: ProgressCb = None,
    should_cancel: ShouldCancel = None,
) -> list[tuple[float, float]]:
    """Where in *ranges* the speaker is reading from the off-camera screen."""
    if not vision_available():
        raise RuntimeError("needs macOS (Apple Vision)")
    faces = sample_faces(src, ranges, progress_cb=progress_cb, should_cancel=should_cancel)
    return reading_stretches(speaker_yaws(faces, sorted(faces)))
