"""Locate DaVinci Resolve and open a Wordly export in it automatically.

Resolve Free only allows scripting from inside the app (external scripting is
Studio-only), so on macOS Wordly:

1. installs ``Wordly Import.lua`` into Resolve's Workspace > Scripts menu
   (Resolve Free lists Lua scripts there but not Python ones),
2. writes a pending-import file naming the new FCPXML timeline,
3. launches Resolve if needed and clicks that menu item via System Events,
4. follows Resolve's own logs until the new project is imported and saved.

Resolve's Lua sandbox cannot write files, so the script cannot report back
directly: success is read from ``davinci_resolve.log`` and script failures
(raised as ``WORDLY_FAILED: ...``) from ``ResolveDebug.txt``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from services.resolve_canvas import DEFAULT_FPS, DEFAULT_HEIGHT, DEFAULT_WIDTH
from utils.paths import ASSETS, TEMP

ProgressCb = Optional[Callable[[float, str], None]]

MAC_RESOLVE_APP_CANDIDATES = (
    Path("/Applications/DaVinci Resolve/DaVinci Resolve.app"),
    Path("/Applications/DaVinci Resolve.app"),
    Path.home() / "Applications" / "DaVinci Resolve" / "DaVinci Resolve.app",
    Path.home() / "Applications" / "DaVinci Resolve.app",
)

IMPORT_SCRIPT_NAME = "Wordly Import"
IMPORT_SCRIPT_TEMPLATE = ASSETS / "resolve_scripts" / f"{IMPORT_SCRIPT_NAME}.lua"
RESOLVE_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Blackmagic Design" / "DaVinci Resolve"
RESOLVE_UTILITY_SCRIPTS_DIR = RESOLVE_SUPPORT_DIR / "Fusion" / "Scripts" / "Utility"
RESOLVE_LOG_DIR = RESOLVE_SUPPORT_DIR / "logs"
PENDING_IMPORT_PATH = TEMP / "resolve_pending_import.lua"

LAUNCH_TIMEOUT_S = 180.0
IMPORT_TIMEOUT_S = 120.0
# Resolve shows the Scripts menu before Fusion can run scripts and silently
# ignores early clicks; re-click when the logs show no sign of the script.
RECLICK_S = 10.0
_POLL_S = 1.5

_FAILED_RE = re.compile(r"WORDLY_FAILED: (.+)")
_SAVING_RE = re.compile(r"Start saving project (.+?)\s*$", re.MULTILINE)


class ResolveOpenError(RuntimeError):
    """Resolve could not be driven automatically; the message says what to do instead."""


def _progress(cb: ProgressCb, ratio: float, message: str) -> None:
    if cb is not None:
        cb(ratio, message)


def find_resolve_app() -> Path | None:
    if sys.platform != "darwin":
        return None
    for candidate in MAC_RESOLVE_APP_CANDIDATES:
        if candidate.is_dir():
            return candidate
    return None


def reveal_in_finder(path: Path) -> None:
    target = path.resolve()
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(target)], start_new_session=True)
        return
    if os.name == "nt":
        subprocess.Popen(["explorer", "/select,", str(target)], start_new_session=True)
        return
    # Linux / WSL — open parent folder
    folder = target if target.is_dir() else target.parent
    subprocess.Popen(["xdg-open", str(folder)], start_new_session=True)


# --- Import script ----------------------------------------------------------


def _lua_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def render_import_script(pending_path: Path | None = None) -> str:
    pending = (pending_path or PENDING_IMPORT_PATH).resolve()
    template = IMPORT_SCRIPT_TEMPLATE.read_text(encoding="utf-8")
    return template.replace("__WORDLY_PENDING_PATH__", _lua_string(str(pending)))


def install_import_script(scripts_dir: Path = RESOLVE_UTILITY_SCRIPTS_DIR) -> Path | None:
    """Install/refresh the Resolve menu script. Returns its path, or None off macOS.

    Resolve scans the Scripts folders at startup, so this runs when Wordly starts
    to be in place before Resolve is launched.
    """
    if sys.platform != "darwin":
        return None
    scripts_dir.mkdir(parents=True, exist_ok=True)
    # Earlier builds installed a Python script, which Resolve Free never lists.
    (scripts_dir / f"{IMPORT_SCRIPT_NAME}.py").unlink(missing_ok=True)
    for legacy in ("resolve_pending_import.json", "resolve_import_status.json"):
        (TEMP / legacy).unlink(missing_ok=True)
    target = scripts_dir / f"{IMPORT_SCRIPT_NAME}.lua"
    content = render_import_script()
    if not target.is_file() or target.read_text(encoding="utf-8") != content:
        target.write_text(content, encoding="utf-8")
    return target


def write_pending_import(timeline: Path, project_name: str, path: Path | None = None) -> str:
    """Write the import request as a Lua table for the Resolve script. Returns its id."""
    path = path or PENDING_IMPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    body = (
        "return {\n"
        f"  id = {_lua_string(request_id)},\n"
        f"  timeline = {_lua_string(str(timeline.resolve()))},\n"
        f"  project_name = {_lua_string(project_name)},\n"
        f"  width = {DEFAULT_WIDTH},\n"
        f"  height = {DEFAULT_HEIGHT},\n"
        f"  fps = {int(DEFAULT_FPS)},\n"
        "}\n"
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return request_id


# --- Resolve logs -------------------------------------------------------------


class _LogTail:
    """Text appended to a log file since construction (restarts if the file shrinks)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = self._size()

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def read_new(self) -> str:
        size = self._size()
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return ""
        with self.path.open("rb") as fh:
            fh.seek(self.offset)
            data = fh.read(size - self.offset)
        self.offset = size
        return data.decode("utf-8", errors="replace")


class ImportWatcher:
    """Follows Resolve's logs for the outcome of one Wordly import."""

    def __init__(self, timeline: Path, project_name: str, log_dir: Path | None = None) -> None:
        log_dir = log_dir or RESOLVE_LOG_DIR
        self.timeline = str(timeline.resolve())
        self.project_name = project_name
        self._main = _LogTail(log_dir / "davinci_resolve.log")
        self._debug = _LogTail(log_dir / "ResolveDebug.txt")
        self.started = False
        self.imported = False

    def poll(self) -> tuple[str, str] | None:
        """("imported", project) or ("failed", message) once known, else None."""
        failure = _FAILED_RE.search(self._debug.read_new())
        if failure:
            return "failed", failure.group(1).strip()
        text = self._main.read_new()
        if f"Current project pointer changed to ({self.project_name}" in text:
            self.started = True
        if f"Start importing {self.timeline}" in text:
            self.started = True
            self.imported = True
        if self.imported:
            for project in _SAVING_RE.findall(text):
                if project.startswith(self.project_name):
                    return "imported", project
        return None


# --- System Events (macOS UI scripting) --------------------------------------


def _osascript(script: str, timeout_s: float = 30.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return 1, "osascript timed out"
    return proc.returncode, (proc.stdout or proc.stderr).strip()


_SCRIPTS_MENU = (
    'menu 1 of menu item "Scripts" of menu 1 of menu bar item "Workspace" of menu bar 1'
)


def _resolve_running() -> bool:
    proc = subprocess.run(
        ["pgrep", "-f", "DaVinci Resolve.app/Contents/MacOS/Resolve"],
        capture_output=True,
    )
    return proc.returncode == 0


def _scripts_menu_items() -> list[str] | None:
    """Names under Workspace > Scripts, or None when Resolve's menu is not reachable."""
    code, out = _osascript(
        f'tell application "System Events" to tell process "Resolve" to get name of every menu item of {_SCRIPTS_MENU}'
    )
    if code != 0:
        # -1719/-1728 just mean Resolve's process or menu isn't up yet; keep polling.
        if "assistive access" in out or "-25211" in out or "-1743" in out:
            raise ResolveOpenError(
                "Wordly needs Accessibility permission to open the project in Resolve.\n"
                "System Settings → Privacy & Security → Accessibility → enable the app "
                "running Wordly (Terminal, VS Code or Python), then click Generate again."
            )
        return None
    return [item.strip() for item in out.split(",")]


def _click_import_menu_item() -> None:
    code, out = _osascript(
        'tell application "System Events" to tell process "Resolve"\n'
        "  set frontmost to true\n"
        f'  click menu item "{IMPORT_SCRIPT_NAME}" of {_SCRIPTS_MENU}\n'
        "end tell",
        timeout_s=IMPORT_TIMEOUT_S,
    )
    if code != 0:
        raise ResolveOpenError(f"Could not click Workspace → Scripts → {IMPORT_SCRIPT_NAME}: {out}")


# --- Public entry point -------------------------------------------------------


def _manual_steps(timeline: Path) -> str:
    return (
        f"In Resolve choose Workspace → Scripts → {IMPORT_SCRIPT_NAME} "
        f"(or File → Import → Timeline… and pick {timeline.name})."
    )


def open_resolve_project(
    timeline_path: Path,
    *,
    project_name: str | None = None,
    progress_cb: ProgressCb = None,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    """Create a Resolve project from *timeline_path* and open it. Returns the project name."""
    timeline = timeline_path.resolve()
    if not timeline.is_file():
        raise FileNotFoundError(f"Timeline file not found: {timeline}")
    name = project_name or timeline.stem

    if sys.platform != "darwin":
        if os.name == "nt":
            os.startfile(timeline)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(timeline)], start_new_session=True)
        return name

    app = find_resolve_app()
    if app is None:
        raise ResolveOpenError(
            "DaVinci Resolve not found under /Applications. Install Resolve Free, then "
            f"import {timeline.name} with File → Import → Timeline…."
        )

    install_import_script()
    write_pending_import(timeline, name)

    was_running = _resolve_running()
    if not was_running:
        _progress(progress_cb, -1.0, "Launching DaVinci Resolve…")
        subprocess.Popen(["open", "-a", str(app)], start_new_session=True)

    _progress(progress_cb, -1.0, "Waiting for DaVinci Resolve to be ready…")
    deadline = time.monotonic() + (LAUNCH_TIMEOUT_S if not was_running else 15.0)
    items: list[str] | None = None
    while time.monotonic() < deadline:
        if should_cancel is not None and should_cancel():
            raise ResolveOpenError("Cancelled. " + _manual_steps(timeline))
        items = _scripts_menu_items()
        if items is not None and IMPORT_SCRIPT_NAME in items:
            break
        time.sleep(_POLL_S)
    else:
        if was_running and items is not None:
            raise ResolveOpenError(
                "Resolve was already open before Wordly installed its import script, so it "
                "isn't in the Scripts menu yet. Quit and reopen Resolve once, then click "
                "Generate again — or " + _manual_steps(timeline)
            )
        raise ResolveOpenError("DaVinci Resolve did not finish starting. " + _manual_steps(timeline))

    _progress(progress_cb, -1.0, "Creating Resolve project and importing timeline…")
    watcher = ImportWatcher(timeline, name)
    deadline = time.monotonic() + IMPORT_TIMEOUT_S
    next_click = 0.0
    while time.monotonic() < deadline:
        if should_cancel is not None and should_cancel():
            raise ResolveOpenError("Cancelled. " + _manual_steps(timeline))
        outcome = watcher.poll()
        if outcome is not None:
            status, detail = outcome
            if status == "failed":
                raise ResolveOpenError(f"Resolve import failed: {detail}")
            subprocess.Popen(["open", "-a", str(app)], start_new_session=True)
            return detail
        if not watcher.started and time.monotonic() >= next_click:
            _click_import_menu_item()
            next_click = time.monotonic() + RECLICK_S
        time.sleep(0.5)
    raise ResolveOpenError("Resolve did not confirm the import. " + _manual_steps(timeline))
