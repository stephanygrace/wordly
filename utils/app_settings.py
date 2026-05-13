"""QSettings keys for Wordly."""

from PySide6.QtCore import QSettings

ORG = "Wordly"
APP = "Wordly"


def settings() -> QSettings:
    return QSettings(ORG, APP)


KEY_LAST_FB_URL = "source/last_facebook_url"
KEY_LAST_COOKIES_FILE = "source/last_ytdlp_cookies_txt"
KEY_USE_IDM = "source/use_idm"
KEY_USE_IDM_WSL_MIGRATION = "source/use_idm_wsl_migration_v1"
