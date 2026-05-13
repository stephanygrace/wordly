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


def _find_filmora_exe() -> Path | None:
    for candidate in FILMORA_EXE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def open_filmora_project(wfp_path: Path) -> None:
    """Open a .wfp file in Wondershare Filmora on Windows or WSL."""
    wfp = wfp_path.resolve()
    if not wfp.is_file():
        raise FileNotFoundError(f"Project file not found: {wfp}")

    if os.name == "nt":
        os.startfile(wfp)  # type: ignore[attr-defined]
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
