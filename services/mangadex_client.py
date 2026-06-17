from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from config import HOME_SECTION_LIMIT, PREFERRED_CHAPTER_LANG, SEARCH_LIMIT
from services.anilist_client import enrich_title_metadata

API_BASE = os.getenv("MANGADEX_API_BASE", "https://api.mangadex.org").rstrip("/")
USER_AGENT = os.getenv(
    "MANGADEX_USER_AGENT",
    "BaltigoMangaBot/2.0 (+https://t.me/BaltigoMangasBot)",
).strip()
TTL = int(os.getenv("MANGADEX_CACHE_TTL", "900") or 900)
READER_TTL = int(os.getenv("MANGADEX_READER_TTL", "21600") or 21600)
TIMEOUT = float(os.getenv("MANGADEX_TIMEOUT", "14") or 14)
MAX_CHAPTERS = int(os.getenv("MANGADEX_MAX_CHAPTERS", "500") or 500)

_CACHE: dict[str, tuple[float, Any]] = {}
_INFLIGHT: dict[str, asyncio.Task] = {}


def _clean(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _norm_lang(value: Any) -> str:
    raw = _clean(value).lower().replace("_", "-")
    if raw in {"ptbr", "pt-br", "br", "pt"}:
        return "pt-br"
    if raw in {"es", "es-la", "es-419", "es-mx"}:
        return "es-la"
    if raw in {"en", "eng"}:
        return "en"
    return raw or PREFERRED_CHAPTER_LANG


def _title_id(value: Any) -> str:
    text = _clean(value)
    return text if text.startswith("md-") else f"md-{text}" if text else ""


def _raw_title_id(value: Any) -> str:
    text = _clean(value)
    return text[3:] if text.startswith("md-") else text


def _chapter_id(value: Any) -> str:
    text = _clean(value)
    return text if text.startswith("mdc-") else f"mdc-{text}" if text else ""


def _raw_chapter_id(value: Any) -> str:
    text = _clean(value)
    return text[4:] if text.startswith("mdc-") else text


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


async def _request(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers, follow_redirects=True) as client:
        resp = await client.get(f"{API_BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()


def _localized(values: dict[str, Any] | None, *preferred: str) -> str:
    if not isinstance(values, dict):
        return ""
    for key in preferred:
        if values.get(key):
            return _clean(values[key])
    for value in values.values():
        if value:
            return _clean(value)
    return ""


def _relationship(item: dict[str, Any], rel_type: str) -> dict[str, Any]:
    for rel in item.get("relationships") or []:
        if isinstance(rel, dict) and rel.get("type") == rel_type:
            return rel
    return {}


def _cover_url(item: dict[str, Any]) -> str:
    # MangaDex covers can occasionally be source-branded placeholders. Baltigo
    # keeps source names invisible, so MangaDex metadata never becomes artwork.
    return ""


def _normal_title(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") or {}
    title = _localized(attrs.get("title"), "pt-br", "en", "ja-ro")
    alt_titles = []
    for alt in attrs.get("altTitles") or []:
        value = _localized(alt, "pt-br", "en", "ja-ro")
        if value and value not in alt_titles:
            alt_titles.append(value)
    tags = []
    for tag in attrs.get("tags") or []:
        name = _localized((tag.get("attributes") or {}).get("name"), "pt-br", "en")
        if name:
            tags.append(name)
    latest = attrs.get("lastChapter") or ""
    return {
        "title_id": _title_id(item.get("id")),
        "source_title_id": item.get("id") or "",
        "title": title or "Mangá",
        "display_title": title or "Mangá",
        "preferred_title": title or "",
        "alt_titles": alt_titles,
        "description": _localized(attrs.get("description"), "pt-br", "en", "es-la"),
        "status": attrs.get("status") or "",
        "genres": tags,
        "cover_url": _cover_url(item),
        "background_url": _cover_url(item),
        "latest_chapter": latest,
        "chapter_number": latest,
        "updated_at": attrs.get("updatedAt") or attrs.get("createdAt") or "",
        "source": "mangadex",
        "sources": ["mangadex"],
    }


async def search_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    q = _clean(query)
    if not q:
        return []

    async def load():
        payload = await _request(
            "/manga",
            {
                "title": q,
                "limit": max(1, min(int(limit or SEARCH_LIMIT), 25)),
                "availableTranslatedLanguage[]": ["pt-br", "en", "es-la"],
                "includes[]": ["cover_art"],
                "order[relevance]": "desc",
                "contentRating[]": ["safe", "suggestive", "erotica", "pornographic"],
            },
        )
        return [_normal_title(item) for item in payload.get("data") or []]

    return await _dedup(f"md-search:{q.lower()}:{limit}", TTL, load)


async def search_titles_fast(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    return await search_titles(query, limit)


def get_cached_search_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]] | None:
    cached = _cache_get(f"md-search:{_clean(query).lower()}:{limit}", TTL)
    return list(cached) if isinstance(cached, list) else None


async def get_title_details(title_ref: str) -> dict[str, Any]:
    raw_id = _raw_title_id(title_ref)

    async def load():
        payload = await _request(f"/manga/{raw_id}", {"includes[]": ["cover_art"]})
        item = _normal_title(payload.get("data") or {})
        try:
            enriched = await enrich_title_metadata(item.get("title") or "")
            item.update({k: v for k, v in enriched.items() if v not in (None, "", [])})
            if not item.get("cover_url") and enriched.get("cover_url_anilist"):
                item["cover_url"] = enriched["cover_url_anilist"]
            if not item.get("background_url"):
                item["background_url"] = item.get("banner_url") or item.get("cover_url") or ""
        except Exception:
            pass
        return item

    return await _dedup(f"md-title:{raw_id}", TTL, load)


async def get_chapter_list(title_id: str, lang: str | None = None) -> dict[str, Any]:
    raw_id = _raw_title_id(title_id)
    resolved_lang = _norm_lang(lang)

    async def load():
        details_task = asyncio.create_task(get_title_details(raw_id))
        active_lang = resolved_lang

        async def _load_lang(feed_lang: str) -> list[dict[str, Any]]:
            chapters: list[dict[str, Any]] = []
            offset = 0
            while offset < MAX_CHAPTERS:
                payload = await _request(
                    f"/manga/{raw_id}/feed",
                    {
                        "limit": min(100, MAX_CHAPTERS - offset),
                        "offset": offset,
                        "translatedLanguage[]": [feed_lang],
                        "order[chapter]": "desc",
                        "includes[]": ["scanlation_group"],
                    },
                )
                data = payload.get("data") or []
                if not data:
                    break
                for item in data:
                    attrs = item.get("attributes") or {}
                    number = _clean(attrs.get("chapter") or attrs.get("title") or "")
                    chap_id = _chapter_id(item.get("id"))
                    group = _relationship(item, "scanlation_group")
                    chapters.append(
                        {
                            "chapter_number": number,
                            "chapter_number_float": number,
                            "chapter_language": feed_lang,
                            "updated_at": attrs.get("publishAt") or attrs.get("updatedAt") or "",
                            "source": "mangadex",
                            "sources": ["mangadex"],
                            "translations": [
                                {
                                    "id": chap_id,
                                    "chapter_id": chap_id,
                                    "language": feed_lang,
                                    "url": f"https://mangadex.org/chapter/{item.get('id')}",
                                    "group_name": (group.get("attributes") or {}).get("name") or "",
                                    "date": attrs.get("publishAt") or "",
                                }
                            ],
                        }
                    )
                offset += len(data)
                total = int(payload.get("total") or 0)
                if offset >= total:
                    break
            return chapters

        chapters = await _load_lang(active_lang)
        if not chapters:
            for fallback_lang in ("en", "es-la", "pt-br"):
                fallback_lang = _norm_lang(fallback_lang)
                if fallback_lang == active_lang:
                    continue
                chapters = await _load_lang(fallback_lang)
                if chapters:
                    active_lang = fallback_lang
                    break
        details = await details_task
        return {
            **details,
            "title_id": _title_id(raw_id),
            "source": "mangadex",
            "sources": ["mangadex"],
            "chapters": chapters,
            "volumes": [],
            "languages": [{"code": active_lang, "label": active_lang.upper()}],
            "total_chapters": len(chapters),
            "source_total_chapters": len(chapters),
        }

    return await _dedup(f"md-chapters:{raw_id}:{resolved_lang}", TTL, load)


async def get_title_bundle(title_ref: str, lang: str | None = None) -> dict[str, Any]:
    return await get_chapter_list(title_ref, lang)


async def get_title_chapters_snapshot(title_ref: str, lang: str | None = None) -> dict[str, Any]:
    return await get_chapter_list(title_ref, lang)


def get_cached_title_bundle(title_ref: str, lang: str | None = None) -> dict[str, Any] | None:
    cached = _cache_get(f"md-chapters:{_raw_title_id(title_ref)}:{_norm_lang(lang)}", TTL)
    return dict(cached) if isinstance(cached, dict) else None


def get_cached_title_summary(title_ref: str) -> dict[str, Any] | None:
    cached = _cache_get(f"md-title:{_raw_title_id(title_ref)}", TTL)
    return dict(cached) if isinstance(cached, dict) else None


def flatten_chapters(chapter_payload: dict[str, Any] | list[Any], preferred_lang: str | None = None, *, ascending: bool = False) -> list[dict[str, Any]]:
    if isinstance(chapter_payload, list):
        chapter_payload = {"chapters": chapter_payload}
    if not isinstance(chapter_payload, dict):
        return []
    title_id = _title_id(chapter_payload.get("title_id") or chapter_payload.get("source_title_id"))
    resolved_lang = _norm_lang(preferred_lang)
    out: list[dict[str, Any]] = []
    for chapter in chapter_payload.get("chapters") or []:
        translations = [item for item in (chapter.get("translations") or []) if isinstance(item, dict)]
        translation = next((item for item in translations if _norm_lang(item.get("language")) == resolved_lang), translations[0] if translations else None)
        if not translation:
            continue
        out.append(
            {
                "chapter_id": translation.get("chapter_id") or translation.get("id") or "",
                "chapter_url": translation.get("url") or "",
                "title_id": title_id,
                "chapter_number": chapter.get("chapter_number") or "",
                "chapter_number_float": chapter.get("chapter_number_float") or chapter.get("chapter_number") or "",
                "chapter_language": translation.get("language") or resolved_lang,
                "group_name": translation.get("group_name") or "",
                "updated_at": translation.get("date") or chapter.get("updated_at") or "",
                "source": "mangadex",
                "sources": ["mangadex"],
            }
        )
    def sort_key(item: dict[str, Any]):
        try:
            return Decimal(str(item.get("chapter_number_float") or item.get("chapter_number") or "0"))
        except Exception:
            return Decimal("-1")
    out.sort(key=sort_key, reverse=not ascending)
    return out


def get_adjacent_chapters(chapter_payload: dict[str, Any], chapter_id: str, preferred_lang: str | None = None):
    chapters = flatten_chapters(chapter_payload, preferred_lang, ascending=True)
    current = _chapter_id(_raw_chapter_id(chapter_id))
    for index, item in enumerate(chapters):
        if item.get("chapter_id") == current:
            return (chapters[index - 1] if index > 0 else None, chapters[index + 1] if index + 1 < len(chapters) else None)
    return None, None


async def get_chapter_reader_payload(chapter_ref: str, lang: str | None = None, title_hint: str = "") -> dict[str, Any]:
    raw_id = _raw_chapter_id(chapter_ref)

    async def load():
        chapter_task = asyncio.create_task(_request(f"/chapter/{raw_id}", {"includes[]": ["manga"]}))
        at_home = await _request(f"/at-home/server/{raw_id}")
        chapter_payload = await chapter_task
        chapter = chapter_payload.get("data") or {}
        attrs = chapter.get("attributes") or {}
        manga_rel = _relationship(chapter, "manga")
        title_id = _title_id(manga_rel.get("id") or title_hint)
        base = at_home.get("baseUrl") or ""
        chap = at_home.get("chapter") or {}
        hash_value = chap.get("hash") or ""
        files = chap.get("dataSaver") or chap.get("data") or []
        images = [f"{base}/data-saver/{hash_value}/{name}" for name in files if base and hash_value and name]
        bundle = await get_chapter_list(title_id, attrs.get("translatedLanguage") or lang)
        prev_ch, next_ch = get_adjacent_chapters(bundle, _chapter_id(raw_id), attrs.get("translatedLanguage") or lang)
        title = _localized((manga_rel.get("attributes") or {}).get("title"), "pt-br", "en") or "MangaDex"
        return {
            "chapter_id": _chapter_id(raw_id),
            "title_id": title_id,
            "title": title,
            "chapter_number": attrs.get("chapter") or "",
            "chapter_language": attrs.get("translatedLanguage") or _norm_lang(lang),
            "images": images,
            "source": "mangadex",
            "sources": ["mangadex"],
            "previous_chapter": prev_ch,
            "next_chapter": next_ch,
        }

    return await _dedup(f"md-reader:{raw_id}", READER_TTL, load)


def get_cached_chapter_reader_payload(chapter_ref: str, lang: str | None = None, title_hint: str = "") -> dict[str, Any] | None:
    cached = _cache_get(f"md-reader:{_raw_chapter_id(chapter_ref)}", READER_TTL)
    return dict(cached) if isinstance(cached, dict) else None


async def get_title_search(search_type: str, limit: int = HOME_SECTION_LIMIT, **extra) -> list[dict[str, Any]]:
    return []


def get_cached_title_search(search_type: str, limit: int = HOME_SECTION_LIMIT, **extra) -> list[dict[str, Any]]:
    return []


async def get_origin_titles(origin: str, limit: int = HOME_SECTION_LIMIT, page: int = 1) -> list[dict[str, Any]]:
    return []


async def get_home_payload(limit: int = HOME_SECTION_LIMIT) -> dict[str, Any]:
    recent = await search_titles("popular", limit)
    return {"featured": recent, "manga": recent, "recommended": recent, "top_viewed": recent}


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


async def get_chapter_details(chapter_ref: str) -> dict[str, Any]:
    return await get_chapter_reader_payload(chapter_ref)


async def get_title_overview(title_ref: str) -> dict[str, Any]:
    return await get_title_details(title_ref)
