from __future__ import annotations

import asyncio
import os
import re
import time
from decimal import Decimal
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from config import HOME_SECTION_LIMIT, PREFERRED_CHAPTER_LANG, SEARCH_LIMIT

BASE_URL = os.getenv("MANGALIVRE_BASE_URL", "https://mangalivre.blog").rstrip("/")
USER_AGENT = os.getenv("MANGALIVRE_USER_AGENT", os.getenv("CATALOG_USER_AGENT", "Mozilla/5.0")).strip()
TTL = int(os.getenv("MANGALIVRE_CACHE_TTL", "900") or 900)
READER_TTL = int(os.getenv("MANGALIVRE_READER_TTL", "21600") or 21600)
TIMEOUT = float(os.getenv("MANGALIVRE_TIMEOUT", "15") or 15)

_CACHE: dict[str, tuple[float, Any]] = {}
_INFLIGHT: dict[str, asyncio.Task] = {}


def _clean(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _absolute(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    if text.startswith("//"):
        return "https:" + text
    return urljoin(BASE_URL + "/", text)


def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    parts = [part for part in path.split("/") if part]
    return parts[-1] if parts else re.sub(r"\W+", "-", url).strip("-")


def _title_id(value: Any) -> str:
    text = _clean(value)
    if text.startswith("ml-"):
        return text
    if text.startswith("http"):
        text = _slug_from_url(text)
    return f"ml-{text}" if text else ""


def _chapter_id(value: Any) -> str:
    text = _clean(value)
    if text.startswith("mlc-"):
        return text
    if text.startswith("http"):
        text = _slug_from_url(text)
    return f"mlc-{text}" if text else ""


def _raw_slug(value: Any) -> str:
    text = _clean(value)
    if text.startswith("mlc-"):
        return text[4:]
    if text.startswith("ml-"):
        return text[3:]
    if text.startswith("http"):
        return _slug_from_url(text)
    return text


def _cache_get(key: str, ttl: int = TTL) -> Any | None:
    item = _CACHE.get(key)
    if not item:
        return None
    created, value = item
    if time.time() - created > ttl:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> Any:
    _CACHE[key] = (time.time(), value)
    return value


async def _dedup(key: str, ttl: int, factory):
    cached = _cache_get(key, ttl)
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


async def _get_text(url: str) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


def _chapter_number(text: Any) -> str:
    match = re.search(r"(?:cap[ií]tulo|chapter|cap\.?)\s*([0-9.]+)", _clean(text), flags=re.I)
    return match.group(1) if match else ""


def _parse_search_card(card) -> dict[str, Any] | None:
    link = card.select_one("a.manga-card-link[href], a[href*='/manga/']")
    href = _absolute(link.get("href") if link else "")
    if not href:
        return None
    title_node = card.select_one(".manga-card-title") or link
    title = " ".join((title_node.get_text(" ", strip=True) if title_node else "").split())
    if not title:
        title = _slug_from_url(href).replace("-", " ").title()
    img = card.select_one("img")
    cover = _absolute((img.get("data-src") or img.get("src")) if img else "")
    genres = [" ".join(node.get_text(" ", strip=True).split()) for node in card.select(".manga-card-genres a, .manga-card-genres span")]
    latest = _chapter_number(card.get_text(" ", strip=True))
    return {
        "title_id": _title_id(href),
        "source_title_id": href,
        "title": title,
        "display_title": title,
        "preferred_title": title,
        "cover_url": cover,
        "background_url": cover,
        "genres": [g for g in genres if g],
        "latest_chapter": latest,
        "chapter_number": latest,
        "source": "mangalivre",
        "sources": ["mangalivre"],
    }


async def search_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    q = _clean(query)
    if not q:
        return []

    async def load():
        html = await _get_text(f"{BASE_URL}/?s={quote_plus(q)}")
        soup = BeautifulSoup(html, "html.parser")
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for card in soup.select(".manga-card"):
            item = _parse_search_card(card)
            if not item:
                continue
            key = item["title_id"]
            if key in seen:
                continue
            seen.add(key)
            results.append(item)
            if len(results) >= limit:
                break
        return results

    return await _dedup(f"ml-search:{q.lower()}:{limit}", TTL, load)


async def search_titles_fast(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    return await search_titles(query, limit)


def get_cached_search_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]] | None:
    cached = _cache_get(f"ml-search:{_clean(query).lower()}:{limit}", TTL)
    return list(cached) if isinstance(cached, list) else None


def _title_url(title_ref: str) -> str:
    raw = _clean(title_ref)
    if raw.startswith("http"):
        return raw
    return f"{BASE_URL}/manga/{_raw_slug(raw).strip('/')}/"


def _chapter_url(chapter_ref: str) -> str:
    raw = _clean(chapter_ref)
    if raw.startswith("http"):
        return raw
    return f"{BASE_URL}/capitulo/{_raw_slug(raw).strip('/')}/"


async def get_title_bundle(title_ref: str, lang: str | None = None) -> dict[str, Any]:
    url = _title_url(title_ref)

    async def load():
        html = await _get_text(url)
        soup = BeautifulSoup(html, "html.parser")
        title = ""
        for node in soup.select("h1, .manga-title, .post-title"):
            title = " ".join(node.get_text(" ", strip=True).split())
            if title:
                break
        if not title:
            title = _slug_from_url(url).replace("-", " ").title()
        cover = ""
        for img in soup.select("img"):
            src = _absolute(img.get("data-src") or img.get("src"))
            if src and "wp-content" in src:
                cover = src
                break
        chapters = []
        seen: set[str] = set()
        for link in soup.select("a[href*='/capitulo/']"):
            href = _absolute(link.get("href"))
            if not href or href in seen:
                continue
            label = " ".join(link.get_text(" ", strip=True).split())
            number = _chapter_number(label or href)
            if not number:
                continue
            seen.add(href)
            chap_id = _chapter_id(href)
            chapters.append(
                {
                    "chapter_number": number,
                    "chapter_number_float": number,
                    "chapter_language": "pt-br",
                    "source": "mangalivre",
                    "sources": ["mangalivre"],
                    "translations": [{"id": chap_id, "chapter_id": chap_id, "language": "pt-br", "url": href}],
                }
            )
        return {
            "title_id": _title_id(url),
            "source_title_id": url,
            "title": title,
            "display_title": title,
            "preferred_title": title,
            "cover_url": cover,
            "background_url": cover,
            "chapters": chapters,
            "volumes": [],
            "languages": [{"code": "pt-br", "label": "PT-BR"}],
            "total_chapters": len(chapters),
            "source_total_chapters": len(chapters),
            "latest_chapter": flatten_chapters({"title_id": _title_id(url), "chapters": chapters}, "pt-br")[0] if chapters else None,
            "source": "mangalivre",
            "sources": ["mangalivre"],
        }

    return await _dedup(f"ml-title:{_raw_slug(url)}", TTL, load)


async def get_chapter_list(title_id: str, lang: str | None = None) -> dict[str, Any]:
    return await get_title_bundle(title_id, lang)


async def get_title_chapters_snapshot(title_ref: str, lang: str | None = None) -> dict[str, Any]:
    return await get_title_bundle(title_ref, lang)


def get_cached_title_bundle(title_ref: str, lang: str | None = None) -> dict[str, Any] | None:
    cached = _cache_get(f"ml-title:{_raw_slug(_title_url(title_ref))}", TTL)
    return dict(cached) if isinstance(cached, dict) else None


def get_cached_title_summary(title_ref: str) -> dict[str, Any] | None:
    return get_cached_title_bundle(title_ref)


def flatten_chapters(chapter_payload: dict[str, Any] | list[Any], preferred_lang: str | None = None, *, ascending: bool = False) -> list[dict[str, Any]]:
    if isinstance(chapter_payload, list):
        chapter_payload = {"chapters": chapter_payload}
    if not isinstance(chapter_payload, dict):
        return []
    title_id = _title_id(chapter_payload.get("title_id") or chapter_payload.get("source_title_id"))
    out = []
    for chapter in chapter_payload.get("chapters") or []:
        translations = [item for item in (chapter.get("translations") or []) if isinstance(item, dict)]
        translation = translations[0] if translations else None
        if not translation:
            continue
        out.append(
            {
                "chapter_id": translation.get("chapter_id") or translation.get("id") or "",
                "chapter_url": translation.get("url") or "",
                "title_id": title_id,
                "chapter_number": chapter.get("chapter_number") or "",
                "chapter_number_float": chapter.get("chapter_number_float") or chapter.get("chapter_number") or "",
                "chapter_language": "pt-br",
                "source": "mangalivre",
                "sources": ["mangalivre"],
            }
        )
    def sort_key(item):
        try:
            return Decimal(str(item.get("chapter_number_float") or "0"))
        except Exception:
            return Decimal("-1")
    out.sort(key=sort_key, reverse=not ascending)
    return out


def get_adjacent_chapters(chapter_payload: dict[str, Any], chapter_id: str, preferred_lang: str | None = None):
    chapters = flatten_chapters(chapter_payload, "pt-br", ascending=True)
    current = _chapter_id(chapter_id)
    for index, item in enumerate(chapters):
        if item.get("chapter_id") == current:
            return (chapters[index - 1] if index > 0 else None, chapters[index + 1] if index + 1 < len(chapters) else None)
    return None, None


async def get_chapter_reader_payload(chapter_ref: str, lang: str | None = None, title_hint: str = "") -> dict[str, Any]:
    url = _chapter_url(chapter_ref)

    async def load():
        html = await _get_text(url)
        soup = BeautifulSoup(html, "html.parser")
        images = []
        seen: set[str] = set()
        for img in soup.select("img"):
            src = _absolute(img.get("data-src") or img.get("src"))
            if not src or src in seen:
                continue
            if "wp-content/uploads" not in src:
                continue
            seen.add(src)
            images.append(src)
        number = _chapter_number(url)
        title = _slug_from_url(url).replace("-", " ").title()
        title_slug = re.sub(r"-capitulo-[0-9.]+.*$", "", _slug_from_url(url), flags=re.I)
        title_id = _title_id(title_hint or f"{BASE_URL}/manga/{title_slug}/")
        bundle = get_cached_title_bundle(title_id) or {}
        prev_ch, next_ch = get_adjacent_chapters(bundle, _chapter_id(url), "pt-br") if bundle else (None, None)
        return {
            "chapter_id": _chapter_id(url),
            "title_id": title_id,
            "title": title,
            "chapter_number": number,
            "chapter_language": "pt-br",
            "images": images,
            "source": "mangalivre",
            "sources": ["mangalivre"],
            "previous_chapter": prev_ch,
            "next_chapter": next_ch,
        }

    return await _dedup(f"ml-reader:{_raw_slug(url)}", READER_TTL, load)


def get_cached_chapter_reader_payload(chapter_ref: str, lang: str | None = None, title_hint: str = "") -> dict[str, Any] | None:
    cached = _cache_get(f"ml-reader:{_raw_slug(_chapter_url(chapter_ref))}", READER_TTL)
    return dict(cached) if isinstance(cached, dict) else None


async def get_title_details(title_ref: str) -> dict[str, Any]:
    return await get_title_bundle(title_ref)


async def get_title_overview(title_ref: str) -> dict[str, Any]:
    return await get_title_bundle(title_ref)


async def get_chapter_details(chapter_ref: str) -> dict[str, Any]:
    return await get_chapter_reader_payload(chapter_ref)


async def get_title_search(search_type: str, limit: int = HOME_SECTION_LIMIT, **extra) -> list[dict[str, Any]]:
    return []


def get_cached_title_search(search_type: str, limit: int = HOME_SECTION_LIMIT, **extra) -> list[dict[str, Any]]:
    return []


async def get_origin_titles(origin: str, limit: int = HOME_SECTION_LIMIT, page: int = 1) -> list[dict[str, Any]]:
    return []


async def get_home_payload(limit: int = HOME_SECTION_LIMIT) -> dict[str, Any]:
    return {}


def get_cached_home_snapshot(limit: int = HOME_SECTION_LIMIT) -> dict[str, Any]:
    return {}


async def get_recent_chapter_updates(limit: int = HOME_SECTION_LIMIT) -> list[dict[str, Any]]:
    return []


async def get_recent_chapters(limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    return []


def get_search_fallback_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    return get_cached_search_titles(query, limit) or []


def clear_catalog_cache() -> None:
    _CACHE.clear()


async def warm_catalog_cache(*, include_home: bool = True) -> None:
    return None


def schedule_warm_catalog_cache():
    return None


def prefetch_title_bundles(title_refs: list[str], *, lang: str | None = None, limit: int = 3):
    async def runner():
        await asyncio.gather(*(get_title_bundle(ref, lang) for ref in title_refs[:limit] if ref), return_exceptions=True)
    try:
        return asyncio.create_task(runner())
    except RuntimeError:
        return None


def prefetch_reader_payloads(chapter_refs: list[str], *, lang: str | None = None, limit: int = 3):
    return None
