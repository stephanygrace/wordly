from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from utils.windows_paths import filmora_media_path

FILMORA_EXE_CANDIDATES = (
    Path(r"C:\Program Files\Wondershare\Wondershare Filmora\Filmora.exe"),
    Path(r"C:\Program Files (x86)\Wondershare\Wondershare Filmora\Filmora.exe"),
    Path("/mnt/c/Program Files/Wondershare/Wondershare Filmora/Filmora.exe"),
    Path("/mnt/c/Program Files (x86)/Wondershare/Wondershare Filmora/Filmora.exe"),
)

MAC_FILMORA_APP_NAMES = (
    "Wondershare Filmora Mac",
    "Wondershare Filmora 15",
    "Wondershare Filmora 14",
    "Wondershare Filmora",
    "Filmora",
)

MAC_FILMORA_BINARY_NAMES = (
    "Wondershare Filmora Mac",
    "Filmora",
)


def _find_filmora_exe() -> Path | None:
    for candidate in FILMORA_EXE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def _find_mac_filmora_app() -> Path | None:
    apps_root = Path("/Applications")
    for name in MAC_FILMORA_APP_NAMES:
        candidate = apps_root / f"{name}.app"
        if candidate.is_dir():
            return candidate
    user_apps = Path.home() / "Applications"
    if user_apps.is_dir():
        for name in MAC_FILMORA_APP_NAMES:
            candidate = user_apps / f"{name}.app"
            if candidate.is_dir():
                return candidate
    return None


def _mac_filmora_binary(app_bundle: Path) -> Path | None:
    macos_dir = app_bundle / "Contents" / "MacOS"
    if not macos_dir.is_dir():
        return None
    for name in MAC_FILMORA_BINARY_NAMES:
        candidate = macos_dir / name
        if candidate.is_file():
            return candidate
    for candidate in sorted(macos_dir.iterdir()):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def open_filmora_project(wfp_path: Path) -> None:
    """Open a .wfp file in Wondershare Filmora (Windows, WSL, or macOS)."""
    wfp = wfp_path.resolve()
    if not wfp.is_file():
        raise FileNotFoundError(f"Project file not found: {wfp}")

    if os.name == "nt":
        filmora = _find_filmora_exe()
        if filmora is not None:
            subprocess.Popen(
                [str(filmora), str(wfp)],
                start_new_session=True,
            )
            return
        os.startfile(wfp)  # type: ignore[attr-defined]
        return

    import sys

    if sys.platform == "darwin":
        filmora_app = _find_mac_filmora_app()
        if filmora_app is not None:
            subprocess.Popen(
                ["open", "-a", str(filmora_app), str(wfp)],
                start_new_session=True,
            )
        else:
            subprocess.Popen(["open", str(wfp)], start_new_session=True)
        return

    win_project = filmora_media_path(wfp)
    filmora = _find_filmora_exe()

    if filmora is not None:
        if str(filmora).startswith("/mnt/"):
            subprocess.Popen([str(filmora), win_project], start_new_session=True)
            return
        subprocess.Popen([str(filmora), str(wfp)], start_new_session=True)
        return

    cmd = shutil.which("cmd.exe")
    if cmd:
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "", win_project],
            start_new_session=True,
        )
        return

    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(wfp))):
        raise RuntimeError("Could not open the project file with the system default app.")
