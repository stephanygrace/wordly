"""Download a photo from a Facebook photo link (or a direct image link) for the reel's end card.

Facebook hides the photo from logged-out visitors but still shares it with link
previews (WhatsApp, Twitter): those get an ``og:image`` tag. The preview URL asks
for a 960 px copy; without that size hint the CDN returns the full upload.
"""

from __future__ import annotations

import hashlib
import html
import re
import urllib.request
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from utils.paths import DOWNLOADS

PHOTOS_DIR = DOWNLOADS / "photos"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
_PREVIEW_AGENTS = (
    "WhatsApp/2.23.20.0",
    "Twitterbot/1.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
)
_OG_IMAGE = re.compile(r'<meta\s+property="og:image"\s+content="([^"]+)"')

Fetch = Callable[[str, str], tuple[bytes, str]]


def _fetch(url: str, user_agent: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(), response.headers.get_content_type()


def _photo_name(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    fbid = (query.get("fbid") or [""])[0]
    if not fbid:
        match = re.search(r"/(\d{8,})/?", urlparse(url).path)
        fbid = match.group(1) if match else ""
    return f"fb_{fbid}" if fbid else "photo_" + hashlib.sha1(url.encode()).hexdigest()[:10]


def _image_extension(content_type: str) -> str:
    return {"image/png": ".png", "image/webp": ".webp"}.get(content_type, ".jpg")


def download_photo(
    url: str,
    *,
    dest_dir: Optional[Path] = None,
    fetch: Optional[Fetch] = None,
) -> Path:
    """Save the photo behind *url* and return its path."""
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("Paste the photo's link (it starts with https://).")
    fetch = fetch or _fetch
    dest_dir = dest_dir or PHOTOS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    image_url = None
    for agent in _PREVIEW_AGENTS:
        try:
            body, content_type = fetch(url, agent)
        except OSError:
            continue
        if content_type.startswith("image/"):
            # A direct image link.
            dest = dest_dir / (_photo_name(url) + _image_extension(content_type))
            dest.write_bytes(body)
            return dest
        match = _OG_IMAGE.search(body.decode("utf-8", errors="ignore"))
        if match:
            image_url = html.unescape(match.group(1))
            break
    if image_url is None:
        raise RuntimeError(
            "Facebook didn't share this photo. Check that the post is public, or save the "
            "photo from Facebook and use Choose image… instead."
        )

    # Full size first (the preview link asks for a smaller copy), then the preview itself.
    for candidate in (re.sub(r"&ctp=[^&]*", "", image_url), image_url):
        try:
            body, content_type = fetch(candidate, "Mozilla/5.0")
        except OSError:
            continue
        if content_type.startswith("image/") and body:
            dest = dest_dir / (_photo_name(url) + _image_extension(content_type))
            dest.write_bytes(body)
            return dest
    raise RuntimeError("Couldn't download the photo from Facebook. Try again in a moment.")
