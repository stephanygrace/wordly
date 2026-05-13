from __future__ import annotations

import json
import re
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from models.project import ProjectState
from services.filmora_14 import FILMORA_BUILD
from services.filmora_template import template_path
from services.trimmer import ffprobe_duration_seconds
from utils.paths import EXPORTS, TEMP
from utils.windows_paths import filmora_media_path

# Media GUIDs from the bundled Filmora 14.2.9 reference project.
TEMPLATE_VIDEO_ID = "{84A7362C-C821-4503-827F-B261E040D2BF}"
TEMPLATE_SOURCE_VIDEO_ID = "{B58CC5D9-D4EA-4091-9FA5-863110B420D0}"
TEMPLATE_AUDIO_ID = "{C19B628D-ACB4-42a5-8DF4-3ADC315A6539}"
TEMPLATE_TIMELINE_ID = "{CE578FD0-98CF-4080-A85C-D05F1DCA0A93}"

MEDIA_TYPE_VIDEO = 8
MEDIA_TYPE_AUDIO = 4


def _guid() -> str:
    return "{" + str(uuid.uuid4()).upper() + "}"


def _us(seconds: float) -> int:
    return int(max(0.0, seconds) * 1_000_000)


def _safe_duration(path: Path, fallback_s: float) -> float:
    try:
        return ffprobe_duration_seconds(path)
    except Exception:
        return fallback_s


def _read_json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"))


def _write_json(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _filmora_path_str(path: Path) -> str:
    """Return a Filmora-friendly Windows path string."""
    win = filmora_media_path(path)
    # WSL UNC paths must keep backslashes — forward slashes break Filmora project open.
    if win.startswith("\\\\"):
        return win
    return win.replace("\\", "/")


def _path_variants(path: str) -> set[str]:
    """Generate common spellings Filmora stores for the same file path."""
    if not path:
        return set()
    variants: set[str] = {path}
    fwd = path.replace("\\", "/")
    back = path.replace("/", "\\")
    variants.add(fwd)
    variants.add(back)
    if fwd.startswith("//"):
        variants.add("\\\\" + fwd[2:].replace("/", "\\"))
    if path.startswith("\\\\"):
        unc_fwd = "//" + path[2:].replace("\\", "/")
        variants.add(unc_fwd)
    if "://" not in path and re.match(r"^[A-Za-z]:", path):
        for base in (fwd, back):
            variants.add(f"file:///{base}")
            variants.add(f"file://{base}")
            variants.add(f"file:/{base}")
    return {v for v in variants if v}


def _register_replacement(replacements: dict[str, str], old: str, new: str) -> None:
    if not old or not new or old == new:
        return
    replacements[old] = new
    for old_var in _path_variants(old):
        for new_var in _path_variants(new):
            if old_var.startswith("file:"):
                if new_var.startswith("file:"):
                    replacements[old_var] = new_var
                else:
                    replacements[old_var] = "file:///" + new_var.lstrip("/")
            else:
                replacements[old_var] = new_var


def _replace_paths_in_text(text: str, replacements: dict[str, str]) -> str:
    out = text
    # Longest keys first so partial paths do not mask full paths.
    for old in sorted(replacements, key=len, reverse=True):
        new = replacements[old]
        if not old or old == new:
            continue
        out = out.replace(old, new)
    return out


def _source_video_path(project: ProjectState, joined_path: Path) -> Path:
    if project.sermon_path and project.sermon_path.exists():
        return project.sermon_path.resolve()
    return joined_path.resolve()


def _patch_project_info(data: dict[str, Any], project: ProjectState, output_path: Path, duration_us: int) -> None:
    now = int(time.time())
    win_out = _filmora_path_str(output_path)
    data["project_file_name"] = project.project_name
    data["project_date_create"] = now
    data["project_date_modify"] = now
    data["project_editor_create_version"] = FILMORA_BUILD
    data["project_editor_modify_version"] = FILMORA_BUILD
    data["project_timeline_duration"] = duration_us
    data["proj_zip_save_path"] = win_out


def _assign_media_item(
    item: dict[str, Any],
    *,
    new_path: Path,
    duration_us: int,
    replacements: dict[str, str],
) -> None:
    old = str(item.get("download_url", "") or item.get("file_name", ""))
    new = _filmora_path_str(new_path)
    item["download_url"] = new
    item["name"] = new_path.stem[:80]
    item["media_length"] = duration_us
    _register_replacement(replacements, old, new)


def _patch_medias_info(
    data: dict[str, Any],
    project: ProjectState,
    *,
    video_path: Path,
    source_video_path: Path,
    audio_path: Path | None,
    video_duration_us: int,
    source_duration_us: int,
    audio_duration_us: int,
) -> dict[str, str]:
    """Update every template video/audio registry entry; return old→new replacements."""
    replacements: dict[str, str] = {}
    items: dict[str, Any] = data.get("media_items", {})

    for media_id, item in items.items():
        media_type = int(item.get("media_type", 0))
        if media_type == MEDIA_TYPE_VIDEO:
            if media_id == TEMPLATE_SOURCE_VIDEO_ID:
                target = source_video_path
                duration = source_duration_us
            else:
                target = video_path
                duration = video_duration_us
            _assign_media_item(
                item,
                new_path=target,
                duration_us=duration,
                replacements=replacements,
            )
        elif media_type == MEDIA_TYPE_AUDIO and audio_path is not None:
            _assign_media_item(
                item,
                new_path=audio_path,
                duration_us=audio_duration_us,
                replacements=replacements,
            )

    if TEMPLATE_TIMELINE_ID in items:
        items[TEMPLATE_TIMELINE_ID]["duration"] = max(video_duration_us, audio_duration_us)
        if project.project_name:
            items[TEMPLATE_TIMELINE_ID]["name"] = project.project_name[:80]

    data["media_items"] = items
    return replacements


def _patch_media_json_files(
    extract_dir: Path,
    *,
    video_path: Path,
    source_video_path: Path,
    audio_path: Path | None,
    replacements: dict[str, str],
) -> None:
    id_to_path: dict[str, Path] = {
        TEMPLATE_VIDEO_ID.strip("{}"): video_path,
        TEMPLATE_SOURCE_VIDEO_ID.strip("{}"): source_video_path,
    }
    if audio_path is not None:
        id_to_path[TEMPLATE_AUDIO_ID.strip("{}")] = audio_path

    for media_json in extract_dir.glob("ProjectFolder/Medias/*/media.json"):
        folder = media_json.parent.name.strip("{}")
        payload = _read_json(media_json.read_bytes())
        old_name = str(payload.get("file_name", ""))

        if folder in id_to_path:
            new_name = _filmora_path_str(id_to_path[folder])
        elif payload.get("sourceInfo", {}).get("basicInfo", {}).get("streamType") == 2:
            # Unknown video media folder — point at joined highlights.
            new_name = _filmora_path_str(video_path)
        elif audio_path is not None and payload.get("sourceInfo", {}).get("audStreamInfos"):
            new_name = _filmora_path_str(audio_path)
        else:
            continue

        payload["file_name"] = new_name
        _register_replacement(replacements, old_name, new_name)
        media_json.write_bytes(_write_json(payload))


def generate_wfp_from_template(
    project: ProjectState,
    *,
    output_path: Path | None = None,
) -> Path:
    template = template_path()
    if not template.is_file():
        raise FileNotFoundError(
            f"Filmora 14.2.9 template missing: {template}. "
            "Save a blank project from Filmora as filmora_14_2_9.wfp."
        )

    if not project.joined_clip_path or not project.joined_clip_path.exists():
        raise ValueError("Joined highlight video is required before exporting a Filmora project.")

    video_path = project.joined_clip_path.resolve()
    source_video_path = _source_video_path(project, video_path)
    audio_path = (
        project.selected_music.local_path.resolve()
        if project.selected_music and project.selected_music.local_path and project.selected_music.local_path.exists()
        else None
    )

    video_duration_us = _us(_safe_duration(video_path, 60.0))
    source_duration_us = _us(_safe_duration(source_video_path, 60.0))
    audio_duration_us = _us(_safe_duration(audio_path, 180.0)) if audio_path else 0
    timeline_duration_us = max(video_duration_us, audio_duration_us)

    EXPORTS.mkdir(parents=True, exist_ok=True)
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in project.project_name.strip()) or "wordly-project"
    out = output_path or (EXPORTS / f"{stem}.wfp")

    extract_dir = TEMP / f"wfp_build_{int(time.time())}"
    extract_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(template) as zf:
        zf.extractall(extract_dir)

    replacements: dict[str, str] = {}

    medias_info_path = extract_dir / "ProjectFolder" / "Medias" / "medias_info.json"
    if medias_info_path.is_file():
        medias_info = _read_json(medias_info_path.read_bytes())
        replacements.update(
            _patch_medias_info(
                medias_info,
                project,
                video_path=video_path,
                source_video_path=source_video_path,
                audio_path=audio_path,
                video_duration_us=video_duration_us,
                source_duration_us=source_duration_us,
                audio_duration_us=audio_duration_us,
            )
        )
        medias_info_path.write_bytes(_write_json(medias_info))

    project_info_path = extract_dir / "ProjectFolder" / "project_info.json"
    if project_info_path.is_file():
        project_info = _read_json(project_info_path.read_bytes())
        _patch_project_info(project_info, project, out, timeline_duration_us)
        project_info_path.write_bytes(_write_json(project_info))

    _patch_media_json_files(
        extract_dir,
        video_path=video_path,
        source_video_path=source_video_path,
        audio_path=audio_path,
        replacements=replacements,
    )

    for path in extract_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".png", ".fsthumb"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        patched = _replace_paths_in_text(text, replacements)
        if project.selected_verse and path.name == "extra.json":
            patched = re.sub(
                r'"TextSentence"\s*:\s*\{\s*"TextSentence"\s*:\s*\[\s*\]',
                '"TextSentence":{"TextSentence":[]}',
                patched,
            )
        if patched != text:
            path.write_text(patched, encoding="utf-8")

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(extract_dir.rglob("*")):
            if file_path.is_file():
                arcname = file_path.relative_to(extract_dir).as_posix()
                zf.write(file_path, arcname)

    project.wfp_output_path = out.resolve()
    return out.resolve()
