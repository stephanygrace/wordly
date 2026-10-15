"""QSettings keys for Wordly."""

from PySide6.QtCore import QSettings

ORG = "Wordly"
APP = "Wordly"


def settings() -> QSettings:
    return QSettings(ORG, APP)


KEY_LAST_FB_URL = "source/last_facebook_url"
KEY_AUTO_CAPTIONS = "export/auto_captions"
KEY_CAPTION_STYLE = "export/caption_style"
KEY_REEL_LAYOUT = "export/reel_layout"
