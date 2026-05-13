from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOWNLOADS = ROOT / "downloads"
CLIPS = ROOT / "clips"
EXPORTS = ROOT / "exports"
TEMP = ROOT / "temp"
ASSETS = ROOT / "assets"


def ensure_directories() -> None:
    for folder in (DOWNLOADS, CLIPS, EXPORTS, TEMP, ASSETS):
        folder.mkdir(parents=True, exist_ok=True)
    for sub in ("filmora_templates", "music"):
        (ASSETS / sub).mkdir(parents=True, exist_ok=True)
