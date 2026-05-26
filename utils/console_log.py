from __future__ import annotations

import os
import sys
from datetime import datetime

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GRAY = "\033[90m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"

_COLOR_ENABLED: bool | None = None
_LAST_PROGRESS: dict[str, tuple[int | None, str]] = {}


def _enable_windows_vt() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def _use_color() -> bool:
    global _COLOR_ENABLED
    if _COLOR_ENABLED is not None:
        return _COLOR_ENABLED
    if os.environ.get("NO_COLOR"):
        _COLOR_ENABLED = False
        return False
    if os.environ.get("FORCE_COLOR"):
        _enable_windows_vt()
        _COLOR_ENABLED = True
        return True
    stream = sys.stdout
    if not getattr(stream, "isatty", lambda: False)():
        _COLOR_ENABLED = False
        return False
    _enable_windows_vt()
    _COLOR_ENABLED = True
    return True


def _c(text: str, *codes: str) -> str:
    if not _use_color() or not codes:
        return text
    return "".join(codes) + text + _RESET


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _prefix(component: str) -> str:
    stamp = _c(f"[{_stamp()}]", _GRAY)
    tag = _c(f"[Wordly:{component}]", _BOLD, _CYAN)
    return f"{stamp} {tag}"


def _print_line(line: str) -> None:
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe, flush=True)


def _should_skip_progress(component: str, message: str, pct: int | None) -> bool:
    prev = _LAST_PROGRESS.get(component)
    if prev is None:
        return False
    prev_pct, prev_msg = prev
    if message != prev_msg:
        return False
    if pct is None or prev_pct is None:
        return True
    return pct // 10 == prev_pct // 10


def log_info(component: str, message: str) -> None:
    _print_line(f"{_prefix(component)} {message}")


def log_step(component: str, message: str) -> None:
    arrow = _c("->", _BOLD, _GREEN)
    _print_line(f"{_prefix(component)} {arrow} {message}")


def log_progress(component: str, message: str, *, ratio: float | None = None) -> None:
    pct: int | None = None
    if ratio is not None and ratio >= 0:
        pct = int(min(100, max(0, round(ratio * 100))))
        if _should_skip_progress(component, message, pct):
            return
        _LAST_PROGRESS[component] = (pct, message)
        pct_s = _c(f"{pct}%", _BOLD, _CYAN)
        _print_line(f"{_prefix(component)} {pct_s} {message}")
        return
    if _should_skip_progress(component, message, None):
        return
    _LAST_PROGRESS[component] = (None, message)
    _print_line(f"{_prefix(component)} {message}")


def log_warn(component: str, message: str) -> None:
    label = _c("WARN", _BOLD, _YELLOW)
    _print_line(f"{_prefix(component)} {label} {message}")


def log_error(component: str, message: str) -> None:
    label = _c("ERROR", _BOLD, _RED)
    _print_line(f"{_prefix(component)} {label} {message}")
