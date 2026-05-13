from __future__ import annotations

import os
import platform
from pathlib import Path


def filmora_media_path(path: Path) -> str:
    """
    Return a path string Filmora 14 on Windows can resolve.

    When Wordly runs in WSL but Filmora runs on the Windows host, Linux paths are
    converted to ``\\\\wsl$\\<distro>\\...`` UNC paths. Paths under ``/mnt/c/`` map
  to drive letters.
    """
    resolved = path.resolve()
    if os.name == "nt":
        return str(resolved)

    text = resolved.as_posix()
    if text.startswith("/mnt/") and len(text) > 6:
        drive = text[5].upper()
        rest = text[7:].replace("/", "\\")
        return f"{drive}:\\{rest}"

    if text.startswith("/home/") or text.startswith("/root/"):
        distro = os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSLENV", "").split(":")[0]
        if not distro:
            distro = "Ubuntu"
        win_tail = text.replace("/", "\\")
        return f"\\\\wsl$\\{distro}{win_tail}"

    return str(resolved)


def filmora_host_note() -> str:
    if os.name == "nt":
        return "Paths are written for Windows Filmora."
    if "microsoft" in platform.release().lower() or os.environ.get("WSL_DISTRO_NAME"):
        distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu")
        return (
            f"Media paths use Windows UNC (\\\\wsl$\\{distro}\\...) so Filmora 14 on "
            "Windows can open files produced in WSL."
        )
    return "Media paths are absolute paths on this machine."
