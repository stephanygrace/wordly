"""QSettings keys for Wordly."""

from PySide6.QtCore import QSettings

ORG = "Wordly"
APP = "Wordly"


def settings() -> QSettings:
    return QSettings(ORG, APP)


KEY_LAST_FB_URL = "source/last_facebook_url"
