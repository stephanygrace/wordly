from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Optional

from models.project import MusicChoice, VerseChoice

_THEME_VERSES: dict[str, list[VerseChoice]] = {
    "faith": [
        VerseChoice("Hebrews 11:1", "Now faith is confidence in what we hope for and assurance about what we do not see."),
        VerseChoice("Mark 11:22", "Have faith in God."),
        VerseChoice("James 1:6", "But when you ask, you must believe and not doubt."),
    ],
    "hope": [
        VerseChoice("Romans 15:13", "May the God of hope fill you with all joy and peace as you trust in him."),
        VerseChoice("Jeremiah 29:11", "For I know the plans I have for you, declares the Lord, plans to prosper you and not to harm you."),
        VerseChoice("Psalm 39:7", "But now, Lord, what do I look for? My hope is in you."),
    ],
    "love": [
        VerseChoice("1 Corinthians 13:4", "Love is patient, love is kind. It does not envy, it does not boast, it is not proud."),
        VerseChoice("1 John 4:19", "We love because he first loved us."),
        VerseChoice("John 3:16", "For God so loved the world that he gave his one and only Son."),
    ],
    "grace": [
        VerseChoice("Ephesians 2:8", "For it is by grace you have been saved, through faith—and this is not from yourselves, it is the gift of God."),
        VerseChoice("2 Corinthians 12:9", "My grace is sufficient for you, for my power is made perfect in weakness."),
        VerseChoice("Titus 2:11", "For the grace of God has appeared that offers salvation to all people."),
    ],
    "peace": [
        VerseChoice("Philippians 4:7", "And the peace of God, which transcends all understanding, will guard your hearts and your minds in Christ Jesus."),
        VerseChoice("John 14:27", "Peace I leave with you; my peace I give you."),
        VerseChoice("Isaiah 26:3", "You will keep in perfect peace those whose minds are steadfast, because they trust in you."),
    ],
    "strength": [
        VerseChoice("Isaiah 40:31", "But those who hope in the Lord will renew their strength."),
        VerseChoice("Philippians 4:13", "I can do all this through him who gives me strength."),
        VerseChoice("Joshua 1:9", "Be strong and courageous. Do not be afraid; do not be discouraged, for the Lord your God will be with you wherever you go."),
    ],
}


def _openai_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def _openai_chat(prompt: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")

    payload = {
        "model": os.environ.get("WORDLY_OPENAI_MODEL", "gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": "You respond with compact JSON only. No markdown fences."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def _fetch_verse_text(reference: str) -> str:
    slug = reference.strip().replace(" ", "%20")
    url = f"https://bible-api.com/{slug}?translation=kjv"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = str(data.get("text") or "").strip()
        if text:
            return re.sub(r"\s+", " ", text)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        pass
    return ""


def _keyword_bucket(theme: str) -> str:
    lower = theme.lower()
    for key in _THEME_VERSES:
        if key in lower:
            return key
    return "faith"


def suggest_bible_verses(theme: str, *, count: int = 3) -> list[VerseChoice]:
    theme = theme.strip()
    if not theme:
        raise ValueError("Enter a theme first.")

    if _openai_configured():
        prompt = (
            f"Theme: {theme}\n"
            f"Return JSON array with exactly {count} objects: "
            '[{"reference":"Book Chapter:Verse","text":"full verse text"}]. '
            "Use well-known Bible verses that match the theme."
        )
        try:
            raw = _openai_chat(prompt)
            parsed = json.loads(raw)
            verses = [
                VerseChoice(str(item["reference"]), str(item["text"]).strip())
                for item in parsed[:count]
                if item.get("reference") and item.get("text")
            ]
            if verses:
                return verses
        except Exception:
            pass

    bucket = _keyword_bucket(theme)
    verses = list(_THEME_VERSES[bucket])
    for ref in ("Romans 8:28", "Psalm 23:1", "Matthew 6:33"):
        if len(verses) >= count:
            break
        text = _fetch_verse_text(ref)
        if text:
            verses.append(VerseChoice(ref, text))
    return verses[:count]


def suggest_instrumentals(theme: str, *, count: int = 4) -> list[MusicChoice]:
    theme = theme.strip()
    if not theme:
        raise ValueError("Enter a theme first.")

    if _openai_configured():
        prompt = (
            f"Theme: {theme}\n"
            f"Return JSON array with exactly {count} instrumental piano/worship beds for church reels. "
            'Each object: {"title":"...", "artist":"...", "search_query":"instrumental piano ..."}. '
            "Prefer royalty-friendly worship instrumentals."
        )
        try:
            raw = _openai_chat(prompt)
            parsed = json.loads(raw)
            choices = [
                MusicChoice(
                    title=str(item["title"]),
                    artist=str(item.get("artist") or ""),
                    search_query=str(item.get("search_query") or f"instrumental piano {item['title']}"),
                )
                for item in parsed[:count]
                if item.get("title")
            ]
            if choices:
                return choices
        except Exception:
            pass

    base = theme.title()
    return [
        MusicChoice("Peaceful Piano Worship", search_query=f"instrumental piano worship {theme}"),
        MusicChoice("Ambient Prayer Bed", search_query=f"ambient instrumental prayer piano {theme}"),
        MusicChoice("Soft Hymn Piano", search_query=f"soft hymn piano instrumental {theme}"),
        MusicChoice("Calm Worship Background", search_query=f"calm worship background music piano {base}"),
    ][:count]
