"""QSettings keys for Wordly desktop preferences."""

from PySide6.QtCore import QSettings

ORG = "Wordly"
APP = "Wordly"


def settings() -> QSettings:
    return QSettings(ORG, APP)


KEY_GEOMETRY = "ui/geometry"
KEY_SPLITTER = "ui/splitter"
KEY_LAST_SERMON_DIR = "paths/last_sermon_parent"
KEY_LAST_SERMON_FILE = "paths/last_sermon_file"
KEY_LAST_MUSIC_DIR = "paths/last_music_parent"
KEY_LAST_MUSIC_FILE = "paths/last_music_file"
KEY_LAST_CLIP_DIR = "paths/last_clip_parent"
KEY_LAST_SUBTITLE_DIR = "paths/last_subtitle_parent"
KEY_LAST_SUBTITLE_FILE = "paths/last_subtitle_file"
KEY_RECENT_SERMONS = "recent/sermon_paths_json"
KEY_LAST_OUTPUT_STEM = "export/last_stem"
KEY_LAST_TRIM_START_TEXT = "trim/last_start_text"
KEY_LAST_TRIM_END_TEXT = "trim/last_end_text"
KEY_LAST_FB_URL = "source/last_facebook_url"
KEY_LAST_COOKIES_FILE = "source/last_ytdlp_cookies_txt"
KEY_LAST_COOKIES_DIR = "source/last_cookies_parent"
KEY_LAST_TEMPLATE_JSON = "export/last_template_json_path"
KEY_LAST_VERSE_REF = "verse/last_reference"
KEY_LAST_VERSE_TEXT = "verse/last_body_plain"
