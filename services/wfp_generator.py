from __future__ import annotations

import json
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from models.project import MusicChoice, ProjectState, VerseChoice
from services.filmora_14 import (
    DEFAULT_AUDIO_CHANNELS,
    DEFAULT_FPS,
    DEFAULT_HEIGHT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_WIDTH,
    FILMORA_BUILD,
    FILMORA_FORMAT,
    FILMORA_PRODUCT,
    FILMORA_VERSION,
    FILMORA_WSVE_VERSION,
)
from services.filmora_14_wfp import generate_wfp_from_template
from services.filmora_template import template_available
from services.trimmer import ffprobe_duration_seconds
from utils.paths import ASSETS, EXPORTS, TEMP
from utils.windows_paths import filmora_media_path


@dataclass(frozen=True)
class WfpLayer:
    layer_id: str
    name: str
    media_path: Path
    track_type: str  # video | audio | text
    start_us: int = 0
    duration_us: int = 0
    text: str = ""
    reference: str = ""


def _new_id() -> str:
    return uuid.uuid4().hex.upper()


def _us(seconds: float) -> int:
    return int(max(0.0, seconds) * 1_000_000)


def _safe_duration(path: Path, fallback_s: float = 60.0) -> float:
    try:
        return ffprobe_duration_seconds(path)
    except Exception:
        return fallback_s


def build_layers(project: ProjectState) -> list[WfpLayer]:
    layers: list[WfpLayer] = []
    sermon = project.sermon_path.resolve() if project.sermon_path and project.sermon_path.is_file() else None
    joined = (
        project.joined_clip_path.resolve()
        if project.joined_clip_path and project.joined_clip_path.exists()
        else None
    )
    if sermon and joined and sermon != joined:
        layers.append(
            WfpLayer(
                layer_id=_new_id(),
                name="Sermon source (segment trims)",
                media_path=sermon,
                track_type="video",
                start_us=0,
                duration_us=_us(_safe_duration(sermon)),
            )
        )
    if project.joined_clip_path and project.joined_clip_path.exists():
        dur = _safe_duration(project.joined_clip_path)
        layers.append(
            WfpLayer(
                layer_id=_new_id(),
                name="Sermon Highlights",
                media_path=project.joined_clip_path.resolve(),
                track_type="video",
                start_us=0,
                duration_us=_us(dur),
            )
        )
    verse: Optional[VerseChoice] = project.selected_verse
    if verse:
        layers.append(
            WfpLayer(
                layer_id=_new_id(),
                name="Bible Verse",
                media_path=Path(""),
                track_type="text",
                start_us=0,
                duration_us=_us(8.0),
                text=verse.text,
                reference=verse.reference,
            )
        )
    music: Optional[MusicChoice] = project.selected_music
    if music and music.local_path and music.local_path.exists():
        dur = _safe_duration(music.local_path, fallback_s=180.0)
        layers.append(
            WfpLayer(
                layer_id=_new_id(),
                name="Instrumental",
                media_path=music.local_path.resolve(),
                track_type="audio",
                start_us=0,
                duration_us=_us(dur),
            )
        )
    return layers


def _media_type_code(track_type: str) -> int:
    return {"video": 1, "audio": 2, "text": 4, "image": 3}.get(track_type, 0)


def _project_info(project: ProjectState, layers: list[WfpLayer], project_guid: str) -> dict[str, Any]:
    now = int(time.time())
    timeline_us = max((layer.duration_us for layer in layers), default=0)
    return {
        "Product": FILMORA_PRODUCT,
        "ProductVersion": FILMORA_BUILD,
        "FormatVersion": FILMORA_WSVE_VERSION,
        "FileType": FILMORA_FORMAT,
        "TargetFilmoraVersion": FILMORA_VERSION,
        "ProjectName": project.project_name,
        "ProjectGUID": project_guid,
        "CreateTimeUTC": now,
        "ModifyTimeUTC": now,
        "Width": DEFAULT_WIDTH,
        "Height": DEFAULT_HEIGHT,
        "FrameRate": DEFAULT_FPS,
        "PixelAspectRatio": 1.0,
        "AudioSampleRate": DEFAULT_SAMPLE_RATE,
        "AudioChannels": DEFAULT_AUDIO_CHANNELS,
        "ColorSpace": "Rec.709",
        "DurationUS": timeline_us,
        "TrackCount": len(layers),
        "Generator": "Wordly",
        "GeneratorNote": "Filmora 14.2.9 layered export — sermon / verse / instrumental on separate tracks.",
    }


def _media_config(layers: list[WfpLayer]) -> dict[str, Any]:
    medias: list[dict[str, Any]] = []
    for layer in layers:
        entry: dict[str, Any] = {
            "MediaID": layer.layer_id,
            "MediaName": layer.name,
            "MediaType": _media_type_code(layer.track_type),
            "DurationUS": layer.duration_us,
            "TimelineStartUS": layer.start_us,
        }
        if layer.track_type == "text":
            entry["TextContent"] = layer.text
            entry["TextTitle"] = layer.reference
            entry["FilePath"] = ""
        else:
            entry["FilePath"] = filmora_media_path(layer.media_path)
            if layer.track_type == "video":
                entry["Width"] = DEFAULT_WIDTH
                entry["Height"] = DEFAULT_HEIGHT
                entry["FrameRate"] = DEFAULT_FPS
        medias.append(entry)
    return {
        "ProductVersion": FILMORA_BUILD,
        "TargetFilmoraVersion": FILMORA_VERSION,
        "MediaCount": len(medias),
        "Medias": medias,
    }


def _timeline_tracks(layers: list[WfpLayer], project_guid: str) -> dict[str, Any]:
    tracks: list[dict[str, Any]] = []
    for idx, layer in enumerate(layers):
        clip: dict[str, Any] = {
            "ClipID": _new_id(),
            "MediaID": layer.layer_id,
            "ClipName": layer.name,
            "StartUS": layer.start_us,
            "DurationUS": layer.duration_us,
            "InPointUS": 0,
            "OutPointUS": layer.duration_us,
            "Enabled": True,
        }
        if layer.track_type == "text":
            clip["Text"] = layer.text
            clip["Title"] = layer.reference
        tracks.append(
            {
                "TrackID": _new_id(),
                "TrackIndex": idx,
                "TrackName": layer.name,
                "TrackType": layer.track_type,
                "Locked": False,
                "Muted": layer.track_type == "audio" and False,
                "Visible": True,
                "Clips": [clip],
            }
        )
    return {
        "ProductVersion": FILMORA_BUILD,
        "TargetFilmoraVersion": FILMORA_VERSION,
        "ProjectGUID": project_guid,
        "DurationUS": max((layer.duration_us for layer in layers), default=0),
        "Tracks": tracks,
    }


def _project_settings(project: ProjectState) -> dict[str, Any]:
    return {
        "ProductVersion": FILMORA_BUILD,
        "TargetFilmoraVersion": FILMORA_VERSION,
        "ProjectName": project.project_name,
        "Theme": project.theme,
        "Resolution": {
            "Width": DEFAULT_WIDTH,
            "Height": DEFAULT_HEIGHT,
            "FrameRate": DEFAULT_FPS,
        },
        "Audio": {
            "SampleRate": DEFAULT_SAMPLE_RATE,
            "Channels": DEFAULT_AUDIO_CHANNELS,
        },
    }


def _wordly_manifest(project: ProjectState, layers: list[WfpLayer]) -> dict[str, Any]:
    return {
        "generator": "wordly",
        "filmora_target": FILMORA_VERSION,
        "filmora_build": FILMORA_BUILD,
        "project": project.project_name,
        "theme": project.theme,
        "layers": [
            {
                "name": layer.name,
                "type": layer.track_type,
                "path": filmora_media_path(layer.media_path) if layer.media_path else "",
            }
            for layer in layers
        ],
    }


def _standalone_entries(project: ProjectState, layers: list[WfpLayer], project_guid: str) -> dict[str, bytes]:
    project_info = _project_info(project, layers, project_guid)
    media_config = _media_config(layers)
    timeline = _timeline_tracks(layers, project_guid)
    settings = _project_settings(project)
    manifest = _wordly_manifest(project, layers)

    return {
        "WSVEFolder/project_info.json": json.dumps(project_info, indent=2).encode("utf-8"),
        "WSVEFolder/Medias/Config.json": json.dumps(media_config, indent=2).encode("utf-8"),
        "WSVEFolder/Timeline/main_timeline.json": json.dumps(timeline, indent=2).encode("utf-8"),
        "WSVEFolder/Project/settings.json": json.dumps(settings, indent=2).encode("utf-8"),
        "WSVEFolder/wordly_manifest.json": json.dumps(manifest, indent=2).encode("utf-8"),
        "WSVEFolder/version.txt": f"{FILMORA_BUILD}\n".encode("utf-8"),
    }


def generate_wfp(
    project: ProjectState,
    *,
    output_path: Optional[Path] = None,
) -> Path:
    """
    Build a Filmora 14.2.9 .wfp project archive.

    When ``assets/filmora_templates/filmora_14_2_9.wfp`` is present, Wordly clones that
    real Filmora 14.2.9 project and patches media paths/durations for the sermon clip and
    instrumental bed while keeping the native timeline layout.
    """
    if template_available():
        return generate_wfp_from_template(project, output_path=output_path)

    layers = build_layers(project)
    if not layers:
        raise ValueError("Nothing to export — complete the earlier wizard steps first.")

    EXPORTS.mkdir(parents=True, exist_ok=True)
    TEMP.mkdir(parents=True, exist_ok=True)
    (ASSETS / "filmora_templates").mkdir(parents=True, exist_ok=True)

    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in project.project_name.strip())
    if not stem:
        stem = "wordly-project"
    out = output_path or (EXPORTS / f"{stem}.wfp")
    project_guid = _new_id()
    entries = _standalone_entries(project, layers, project_guid)

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)

    project.wfp_output_path = out.resolve()
    return out.resolve()
