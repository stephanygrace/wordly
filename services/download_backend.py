from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from utils.console_log import log_info, log_step
from utils.windows_paths import filmora_media_path

ProgressCallback = Callable[[float, str], None]
ShouldCancel = Callable[[], bool]

IDM_WINDOWS_PATHS = (
    Path(r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe"),
    Path(r"C:\Program Files\Internet Download Manager\IDMan.exe"),
)

IDM_WSL_PATHS = (
    Path("/mnt/c/Program Files (x86)/Internet Download Manager/IDMan.exe"),
    Path("/mnt/c/Program Files/Internet Download Manager/IDMan.exe"),
)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".m4v", ".mov", ".flv", ".ts", ".mpeg", ".mpg", ".3gp"}
INCOMPLETE_SUFFIXES = (".part", ".crdownload", ".tmp", ".partial", ".download")


def _idm_candidate_paths() -> tuple[Path, ...]:
    if os.name == "nt":
        return IDM_WINDOWS_PATHS
    return IDM_WSL_PATHS + IDM_WINDOWS_PATHS


def find_idm_executable() -> Optional[Path]:
    for candidate in _idm_candidate_paths():
        if candidate.is_file():
            return candidate
    return None


def _idm_path_for_launch(idm: Path) -> str:
    """Executable path subprocess can invoke from the current OS environment."""
    return str(idm.resolve())


def _idm_output_dir_for_launch(output_dir: Path) -> str:
    """Folder path IDM on Windows can write to."""
    if os.name == "nt":
        return str(output_dir.resolve())
    return filmora_media_path(output_dir)


def idm_watch_dirs(output_dir: Path) -> list[Path]:
    """Folders to poll for a finished IDM download."""
    dirs: list[Path] = [output_dir.resolve()]
    seen = {dirs[0]}
    if os.name != "nt":
        users_root = Path("/mnt/c/Users")
        if users_root.is_dir():
            for pattern in ("Downloads", "Desktop", "Videos"):
                for folder in users_root.glob(f"*/{pattern}"):
                    if folder.is_dir():
                        resolved = folder.resolve()
                        if resolved not in seen:
                            dirs.append(resolved)
                            seen.add(resolved)
    return dirs


def find_aria2c() -> Optional[str]:
    return shutil.which("aria2c")


def idm_available() -> bool:
    return find_idm_executable() is not None


def aria2c_available() -> bool:
    return find_aria2c() is not None


def _is_video_file(path: Path) -> bool:
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return False
    lower_name = path.name.lower()
    return not any(lower_name.endswith(suffix) for suffix in INCOMPLETE_SUFFIXES)


def _snapshot_video_files(folder: Path) -> dict[Path, tuple[float, int]]:
    out: dict[Path, tuple[float, int]] = {}
    if not folder.is_dir():
        return out
    for path in folder.iterdir():
        if not _is_video_file(path):
            continue
        try:
            stat = path.stat()
            out[path.resolve()] = (stat.st_mtime, stat.st_size)
        except OSError:
            continue
    return out


def snapshot_watch_dirs(watch_dirs: list[Path]) -> dict[Path, tuple[float, int]]:
    merged: dict[Path, tuple[float, int]] = {}
    for folder in watch_dirs:
        merged.update(_snapshot_video_files(folder))
    return merged


def import_video_to_folder(path: Path, output_dir: Path) -> Path:
    """Move a finished IDM file into Wordly's downloads folder if needed."""
    source = path.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if source.parent == output_dir:
        return source
    dest = output_dir / source.name
    if dest.exists():
        stem = source.stem
        dest = output_dir / f"{stem}_{int(time.time())}{source.suffix}"
    shutil.move(str(source), str(dest))
    log_info("idm", f"Imported IDM download → {dest}")
    return dest.resolve()


def _candidate_changed(
    path: Path,
    size: int,
    mtime: float,
    before: dict[Path, tuple[float, int]],
) -> bool:
    if size < 1024 * 50:
        return False
    prev = before.get(path.resolve())
    if prev is None:
        return True
    prev_mtime, prev_size = prev
    return size > prev_size or mtime > prev_mtime


def wait_for_idm_video(
    output_dir: Path,
    watch_dirs: list[Path],
    before: dict[Path, tuple[float, int]],
    *,
    timeout_s: float = 7200.0,
    should_cancel: Optional[ShouldCancel] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> Path:
    deadline = time.monotonic() + timeout_s
    stable: dict[Path, tuple[int, float]] = {}
    logged_dirs = False
    while time.monotonic() < deadline:
        if should_cancel and should_cancel():
            raise RuntimeError("Cancelled")
        if not logged_dirs:
            joined = ", ".join(str(d) for d in watch_dirs)
            log_info("idm", f"Watching for finished download in: {joined}")
            logged_dirs = True
        for folder in watch_dirs:
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                if not _is_video_file(path):
                    continue
                resolved = path.resolve()
                try:
                    stat = path.stat()
                    size = stat.st_size
                    mtime = stat.st_mtime
                except OSError:
                    continue
                if not _candidate_changed(resolved, size, mtime, before):
                    continue
                prev = stable.get(resolved)
                if prev and prev[0] == size:
                    if progress_cb:
                        progress_cb(-1.0, f"IDM download complete — {path.name}")
                    final_path = import_video_to_folder(resolved, output_dir)
                    log_info("idm", f"Using sermon file: {final_path}")
                    return final_path
                stable[resolved] = (size, mtime)
                if progress_cb:
                    progress_cb(-1.0, f"IDM downloading… {path.name} ({size // (1024 * 1024)} MiB)")
        if progress_cb:
            progress_cb(-1.0, "Waiting for Internet Download Manager to finish…")
        time.sleep(1.5)
    raise TimeoutError(
        "Timed out waiting for IDM to finish. Check IDM, then look in Windows Downloads "
        f"or {output_dir.resolve()}."
    )


def download_with_idm(
    url: str,
    output_dir: Path,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    should_cancel: Optional[ShouldCancel] = None,
    suggested_filename: Optional[str] = None,
) -> Path:
    idm = find_idm_executable()
    if idm is None:
        raise RuntimeError("Internet Download Manager was not found on this system.")

    output_dir.mkdir(parents=True, exist_ok=True)
    watch_dirs = idm_watch_dirs(output_dir)
    before = snapshot_watch_dirs(watch_dirs)
    if progress_cb:
        progress_cb(-1.0, "Sending stream URL to Internet Download Manager…")
    log_step("idm", f"Launching IDMan.exe for URL: {url[:160]}")
    log_info("idm", f"Wordly downloads folder: {output_dir.resolve()}")

    idm_launch = _idm_path_for_launch(idm)
    save_folder = _idm_output_dir_for_launch(output_dir)
    log_info("idm", f"IDM save folder: {save_folder}")

    cmd = [
        idm_launch,
        "/d",
        url,
        "/p",
        save_folder,
        "/n",
        "/s",
    ]
    if suggested_filename:
        cmd.extend(["/f", suggested_filename])
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CETACHED_PROCESS  # type: ignore[attr-defined]
    subprocess.Popen(cmd, creationflags=creationflags)

    return wait_for_idm_video(
        output_dir,
        watch_dirs,
        before,
        should_cancel=should_cancel,
        progress_cb=progress_cb,
    )
