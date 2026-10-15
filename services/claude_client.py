"""Anthropic API key lookup/storage and the Claude client for Wordly.

The key comes from, in order: the ANTHROPIC_API_KEY environment variable, the
project's ``.env`` file (git-ignored, readable only by you), or the macOS
Keychain. The wizard saves keys to ``.env``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from utils.paths import ROOT

ENV_FILE = ROOT / ".env"
ENV_NAME = "ANTHROPIC_API_KEY"
# Needed only for organisation keys that aren't tied to a workspace.
WORKSPACE_ENV_NAME = "ANTHROPIC_WORKSPACE_ID"
KEYCHAIN_SERVICE = "Wordly"
KEYCHAIN_ACCOUNT = "anthropic-api-key"


class MissingApiKey(RuntimeError):
    """No Anthropic API key is configured."""


def _read_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=value lines, # comments, optional quotes."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def _keychain_key() -> str | None:
    if sys.platform != "darwin":
        return None
    found = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
        capture_output=True,
        text=True,
    )
    return (found.stdout.strip() or None) if found.returncode == 0 else None


def stored_api_key(env_file: Path | None = None) -> str | None:
    return _read_env_file(env_file or ENV_FILE).get(ENV_NAME, "").strip() or _keychain_key()


def save_api_key(key: str, env_file: Path | None = None) -> Path:
    """Write the key to the project's .env (other lines kept), readable only by you."""
    key = key.strip()
    if not key:
        raise ValueError("The API key is empty.")
    path = env_file or ENV_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    kept = [ln for ln in lines if ln.split("=", 1)[0].strip().removeprefix("export ").strip() != ENV_NAME]
    if not kept:
        kept = ["# Wordly secrets — this file is git-ignored; never commit it."]
    kept.append(f"{ENV_NAME}={key}")
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def api_key(env_file: Path | None = None) -> str:
    key = os.environ.get(ENV_NAME, "").strip() or stored_api_key(env_file)
    if not key:
        raise MissingApiKey(
            f"No Anthropic API key. Add {ENV_NAME}=sk-ant-… to the project's .env file "
            "(or paste it when Wordly asks)."
        )
    return key


def workspace_id(env_file: Path | None = None) -> str | None:
    value = os.environ.get(WORKSPACE_ENV_NAME, "").strip()
    return value or _read_env_file(env_file or ENV_FILE).get(WORKSPACE_ENV_NAME, "").strip() or None


def client():
    import anthropic

    headers = {"anthropic-workspace-id": ws} if (ws := workspace_id()) else None
    return anthropic.Anthropic(api_key=api_key(), default_headers=headers)
