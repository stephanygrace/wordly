from __future__ import annotations

import os
import subprocess


def background_creationflags() -> int:
    """Windows flags for spawning a child process without a flashing console window."""
    if os.name != "nt":
        return 0
    flags = 0
    detached = getattr(subprocess, "DETACHED_PROCESS", None)
    if detached is not None:
        flags |= int(detached)
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", None)
    if no_window is not None:
        flags |= int(no_window)
    return flags
