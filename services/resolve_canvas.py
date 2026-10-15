"""Timeline canvas defaults for DaVinci Resolve Free exports."""

from __future__ import annotations

DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_FPS = 30.0
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_AUDIO_CHANNELS = 2

# FCPXML frame duration for 30 fps (100/3000s per frame).
FCPXML_FRAME_DURATION = "100/3000s"
FCPXML_VERSION = "1.9"

# Auto-caption titles are FCPXML Basic Titles (Resolve imports them as Text
# generators); their look comes from services/caption_style.py.
CAPTION_TITLE_UID = ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"
# Vertical distance between two caption lines, in percent of frame height per
# font-size point (measured in Resolve 21 at 1080x1920: ~6.4% at size 64).
CAPTION_LINE_GAP_PER_POINT = 0.1
