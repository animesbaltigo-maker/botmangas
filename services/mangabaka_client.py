from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

import httpx

API_BASE = os.getenv("MANGABAKA_API_BASE", "https://api.mangabaka.dev").rstrip("/")
TIMEOUT = float(os.getenv("MANGABAKA_TIMEOUT", "8") or 8)
TTL = int(os.getenv("MANGABAKA_CACHE_TTL", "7200") or 7200)
USER_AGENT = os.getenv("MANGABAKA_USER_AGENT", "MangasBaltigo/1.0").strip()

_CACHE: dict[str, tuple[float, Any]] = {}
_INFLIGHT: dict[str, asyncio.Task] = {}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _cache_get(key: str) -> Any | None:
    item = _CACHE.get(key)
    if not item:
        return None
    created, value = item
    if time.time() - created > TTL:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> Any:
    _CACHE[key] = (time.time(), value)
    return value


async def _dedup(key: str, factory):
    cached = _cache_get(key)
    if cached is not None:
        return cached
    task = _INFLIGHT.get(key)
    if task:
        return await task
    task = asyncio.create_task(factory())
    _INFLIGHT[key] = task
    try:
        return _cache_set(key, await task)
    finally:
        _INFLIGHT.pop(key, None)


def _cover_url(item: dict[str, Any]) -> str:
    cover = item.get("cover") or {}
    if isinstance(cover, dict):
        raw = cover.get("raw") or {}
        if isinstance(raw, dict) and raw.get("url"):
            return _clean(raw.get("url"))
        for size in ("x350", "x250", "x150"):
            sized = cover.get(size) or {}
            if isinstance(sized, dict):
                for scale in ("x2", "x1", "x3"):
                    if sized.get(scale):
                        return _clean(sized.get(scale))
    return ""


def _genres(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("genres", "tags"):
        for raw in item.get(key) or []:
            if isinstance(raw, str):
                value = _clean(raw)
            elif isinstance(raw, dict):
                value = _clean(raw.get("name") or raw.get("title") or raw.get("slug"))
            else:
                value = ""
            if value and value not in values:
                values.append(value)
    for raw in item.get("genres_v2") or []:
        value = _clean(raw.get("name") if isinstance(raw, dict) else raw)
        if value and value not in values:
            values.append(value)
    return values[:10]


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    publishers = []
    for raw in item.get("publishers") or []:
        if isinstance(raw, str):
            publishers.append(_clean(raw))
        elif isinstance(raw, dict):
            publishers.append(_clean(raw.get("name") or raw.get("title")))

    links = item.get("links") or {}
    if not isinstance(links, dict):
        links = {}

    return {
        "id": item.get("id"),
        "title": _clean(item.get("title")),
        "native_title": _clean(item.get("native_title")),
        "romanized_title": _clean(item.get("romanized_title")),
        "type": _clean(item.get("type")),
        "status": _clean(item.get("status")),
        "year": item.get("year") or "",
        "description": _clean(item.get("description")),
        "authors": [_clean(x) for x in (item.get("authors") or []) if _clean(x)],
        "artists": [_clean(x) for x in (item.get("artists") or []) if _clean(x)],
        "genres": _genres(item),
        "rating": item.get("rating") or "",
        "content_rating": _clean(item.get("content_rating")),
        "total_chapters": item.get("total_chapters") or "",
        "final_volume": item.get("final_volume") or "",
        "is_licensed": bool(item.get("is_licensed")),
        "has_anime": bool(item.get("has_anime")),
        "publishers": [x for x in publishers if x],
        "site_url": f"https://mangabaka.org/series/{item.get('id')}" if item.get("id") else "",
        "cover_url": _cover_url(item),
        "source": "mangabaka",
    }


async def search_series(query: str, limit: int = 3) -> list[dict[str, Any]]:
    q = _clean(query)
    if not q:
        return []
    limit = max(1, min(int(limit or 3), 10))
    key = f"mbaka-search:{q.lower()}:{limit}"

    async def load():
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers, follow_redirects=True) as client:
            response = await client.get(f"{API_BASE}/v1/series/search", params={"q": q, "limit": limit})
            response.raise_for_status()
            payload = response.json()
        return [_normalize_item(item) for item in (payload.get("data") or []) if isinstance(item, dict)]

    return await _dedup(key, load)


def format_for_prompt(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items[:3]:
        title = item.get("title")
        if not title:
            continue
        extras = [
            f"tipo {item.get('type')}" if item.get("type") else "",
            f"status {item.get('status')}" if item.get("status") else "",
            f"ano {item.get('year')}" if item.get("year") else "",
            f"capitulos {item.get('total_chapters')}" if item.get("total_chapters") else "",
            f"nota {item.get('rating')}" if item.get("rating") else "",
            f"autores {', '.join(item.get('authors') or [])}" if item.get("authors") else "",
            f"artistas {', '.join(item.get('artists') or [])}" if item.get("artists") else "",
            f"generos {', '.join(item.get('genres') or [])}" if item.get("genres") else "",
        ]
        line = f"- {title}: " + "; ".join(x for x in extras if x)
        description = _clean(item.get("description"))
        if description:
            line += f"; sinopse {description[:700]}"
        lines.append(line)
    return "\n".join(lines)
