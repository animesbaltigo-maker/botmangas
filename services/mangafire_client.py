import asyncio
import base64
import contextlib
import hashlib
import html
import json
import math
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from config import (
    AUTO_POST_LIMIT,
    DATA_DIR,
    HOME_SECTION_LIMIT,
    PREFERRED_CHAPTER_LANG,
    SEARCH_LIMIT,
    WEBAPP_BASE_URL,
)
from services.anilist_client import enrich_title_metadata

BASE_URL = "https://mangafire.to"
STATIC_BASE = "https://static.mfcdn.nl"
USER_AGENT = os.getenv(
    "MANGAFIRE_USER_AGENT",
    os.getenv(
        "CATALOG_USER_AGENT",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    ),
).strip()
BROWSER_USER_AGENT = os.getenv(
    "MANGAFIRE_BROWSER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
).strip()

TITLE_TTL = int(os.getenv("MANGAFIRE_TITLE_TTL", "1800") or 1800)
CHAPTERS_TTL = int(os.getenv("MANGAFIRE_CHAPTERS_TTL", "1200") or 1200)
SEARCH_TTL = int(os.getenv("MANGAFIRE_SEARCH_TTL", "900") or 900)
READER_TTL = int(os.getenv("MANGAFIRE_READER_TTL", "21600") or 21600)
HOME_TTL = int(os.getenv("MANGAFIRE_HOME_TTL", "900") or 900)
MANGABAKA_API_BASE = os.getenv("MANGABAKA_API_BASE", "https://api.mangabaka.dev/v1").rstrip("/")
MANGABAKA_SITE_BASE = os.getenv("MANGABAKA_SITE_BASE", "https://mangabaka.org").rstrip("/")
MANGABAKA_TTL = int(os.getenv("MANGABAKA_TTL", "86400") or 86400)
MANGABAKA_TIMEOUT = float(os.getenv("MANGABAKA_TIMEOUT", "2.5") or 2.5)
MANGABAKA_BATCH_NETWORK_LIMIT = int(os.getenv("MANGABAKA_BATCH_NETWORK_LIMIT", "5") or 5)

MAP_PATH = DATA_DIR / "source_id_map.json"
TITLE_SUMMARY_PATH = DATA_DIR / "title_summary_cache.json"

MF_TITLE_RE = re.compile(r"^mf-[a-z0-9]+$", re.I)
MF_CHAPTER_RE = re.compile(r"^mf-(?P<hid>[a-z0-9]+)-(?P<lang>[a-z0-9-]+)-(?P<kind>chapter|volume)-(?P<number>[0-9.]+)$", re.I)
LEGACY_TITLE_RE = re.compile(r"^[a-f0-9]{20,32}$", re.I)

_CACHE: dict[str, tuple[float, Any]] = {}
_INFLIGHT: dict[str, asyncio.Task] = {}
_TITLE_SUMMARY_CACHE: dict[str, dict[str, Any]] | None = None
_SOURCE_MAP: dict[str, Any] | None = None
_CHAPTER_TITLE_CACHE: dict[str, str] = {}
_VRF_CACHE: dict[str, tuple[float, str]] = {}
_CHAPTER_URL_CACHE: dict[str, str] = {}
_CHAPTER_SOURCE_ID_CACHE: dict[str, str] = {}


def _add8(n: int):
    return lambda c: (c + n) & 255


def _sub8(n: int):
    return lambda c: (c - n + 256) & 255


def _rotl8(n: int):
    return lambda c: ((c << n) | (c >> (8 - n))) & 255


def _rotr8(n: int):
    return lambda c: ((c >> n) | (c << (8 - n))) & 255


_VRF_STAGES = (
    (
        "FgxyJUQDPUGSzwbAq/ToWn4/e8jYzvabE+dLMb1XU1o=",
        "yH6MXnMEcDVWO/9a6P9W92BAh1eRLVFxFlWTHUqQ474=",
        "l9PavRg=",
        (_sub8(223), _rotr8(4), _rotr8(4), _add8(234), _rotr8(7), _rotr8(2), _rotr8(7), _sub8(223), _rotr8(7), _rotr8(6)),
    ),
    (
        "CQx3CLwswJAnM1VxOqX+y+f3eUns03ulxv8Z+0gUyik=",
        "RK7y4dZ0azs9Uqz+bbFB46Bx2K9EHg74ndxknY9uknA=",
        "Ml2v7ag1Jg==",
        (_add8(19), _rotr8(7), _add8(19), _rotr8(6), _add8(19), _rotr8(1), _add8(19), _rotr8(6), _rotr8(7), _rotr8(4)),
    ),
    (
        "fAS+otFLkKsKAJzu3yU+rGOlbbFVq+u+LaS6+s1eCJs=",
        "rqr9HeTQOg8TlFiIGZpJaxcvAaKHwMwrkqojJCpcvoc=",
        "i/Va0UxrbMo=",
        (_sub8(223), _rotr8(1), _add8(19), _sub8(223), _rotl8(2), _sub8(223), _add8(19), _rotl8(1), _rotl8(2), _rotl8(1)),
    ),
    (
        "Oy45fQVK9kq9019+VysXVlz1F9S1YwYKgXyzGlZrijo=",
        "/4GPpmZXYpn5RpkP7FC/dt8SXz7W30nUZTe8wb+3xmU=",
        "WFjKAHGEkQM=",
        (_add8(19), _rotl8(1), _rotl8(1), _rotr8(1), _add8(234), _rotl8(1), _sub8(223), _rotl8(6), _rotl8(4), _rotl8(1)),
    ),
    (
        "aoDIdXezm2l3HrcnQdkPJTDT8+W6mcl2/02ewBHfPzg=",
        "wsSGSBXKWA9q1oDJpjtJddVxH+evCfL5SO9HZnUDFU8=",
        "5Rr27rWd",
        (_rotr8(1), _rotl8(1), _rotl8(6), _rotr8(1), _rotl8(2), _rotr8(4), _rotl8(1), _rotl8(1), _sub8(223), _rotl8(2)),
    ),
)


def _clean(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_lang(value: Any) -> str:
    raw = _clean(value).lower().replace("_", "-")
    if raw in {"ptbr", "pt-br", "br"}:
        return "pt-br"
    if raw in {"pt", "pt-pt"}:
        return "pt"
    if raw in {"es-la", "es-419", "es-mx"}:
        return "es-la"
    return raw or PREFERRED_CHAPTER_LANG


def _mf_title_id(hid: str) -> str:
    hid = _clean(hid).lower()
    return f"mf-{hid}" if hid and not hid.startswith("mf-") else hid


def _hid_from_title_id(value: Any) -> str:
    text = _clean(value)
    if text.startswith("mf-"):
        return text[3:].lower()
    match = re.search(r"/manga/[^./]+\.([a-z0-9]+)", text, flags=re.I)
    if match:
        return match.group(1).lower()
    match = re.search(r"\.([a-z0-9]{4,12})(?:[/#?]|$)", text, flags=re.I)
    return match.group(1).lower() if match else ""


def _rc4(key: bytes, input_data: bytes | list[int]) -> list[int]:
    lkey = len(key)
    j = 0
    state = list(range(256))
    for i in range(256):
        j = (j + state[i] + key[i % lkey]) & 255
        state[i], state[j] = state[j], state[i]
    output = []
    i = j = 0
    for c in input_data:
        i = (i + 1) & 255
        j = (j + state[i]) & 255
        state[i], state[j] = state[j], state[i]
        k = state[(state[i] + state[j]) & 255]
        output.append(c ^ k)
    return output


def _vrf_transform(input_data: list[int], seed: bytes, prefix: bytes, schedule) -> list[int]:
    output = []
    prefix_len = len(prefix)
    for idx, c in enumerate(input_data):
        if idx < prefix_len:
            output.append(prefix[idx] or 0)
        output.append(schedule[idx % 10]((c ^ seed[idx % 32]) & 255) & 255)
    return output


def _generate_vrf(value: Any) -> str:
    data: bytes | list[int] = quote(_clean(value)).encode()
    for key_b64, seed_b64, prefix_b64, schedule in _VRF_STAGES:
        data = _vrf_transform(
            _rc4(base64.b64decode(key_b64), data),
            base64.b64decode(seed_b64),
            base64.b64decode(prefix_b64),
            schedule,
        )
    return base64.b64encode(bytes(data)).rstrip(b"=").replace(b"+", b"-").replace(b"/", b"_").decode()


def _chapter_id(hid: str, lang: str, kind: str, number: Any) -> str:
    safe_number = _clean(number).replace("/", ".")
    return f"mf-{hid}-{_norm_lang(lang)}-{kind}-{safe_number}"


def _parse_chapter_id(value: Any) -> dict[str, str]:
    text = _clean(value)
    match = MF_CHAPTER_RE.match(text)
    if match:
        return match.groupdict()
    parsed = urlparse(text)
    path = parsed.path if parsed.scheme else text
    match = re.search(r"/read/[^.]+\.([a-z0-9]+)/([^/]+)/(chapter|volume)-([0-9.]+)", path, flags=re.I)
    if not match:
        return {}
    return {"hid": match.group(1), "lang": match.group(2), "kind": match.group(3), "number": match.group(4)}


def _absolute(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    if text.startswith("//"):
        return "https:" + text
    return urljoin(BASE_URL + "/", text)


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _clean(value).lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _cache_get(key: str, ttl: int) -> Any | None:
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
        value = await task
        return _cache_set(key, value)
    finally:
        _INFLIGHT.pop(key, None)


def clear_catalog_cache() -> None:
    _CACHE.clear()
    _VRF_CACHE.clear()


def _headers(referer: str | None = None) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": referer or f"{BASE_URL}/",
    }


async def _request_text(url: str, *, referer: str | None = None, timeout: float = 30.0) -> str:
    async with httpx.AsyncClient(headers=_headers(referer), follow_redirects=True, timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


async def _request_json(url: str, *, referer: str | None = None, timeout: float = 30.0) -> dict[str, Any]:
    async with httpx.AsyncClient(headers={**_headers(referer), "Accept": "application/json,*/*", "X-Requested-With": "XMLHttpRequest"}, follow_redirects=True, timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


def _load_title_summaries() -> dict[str, dict[str, Any]]:
    global _TITLE_SUMMARY_CACHE
    if _TITLE_SUMMARY_CACHE is not None:
        return _TITLE_SUMMARY_CACHE
    try:
        raw = json.loads(TITLE_SUMMARY_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    _TITLE_SUMMARY_CACHE = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    return _TITLE_SUMMARY_CACHE


def _remember_title_summary(item: dict[str, Any]) -> None:
    title_id = _clean(item.get("title_id"))
    if not title_id:
        return
    summaries = _load_title_summaries()
    current = summaries.get(title_id) or {}
    merged = {**current, **{k: v for k, v in item.items() if v not in (None, "", [])}}
    summaries[title_id] = merged
    try:
        TITLE_SUMMARY_PATH.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_source_map() -> dict[str, Any]:
    global _SOURCE_MAP
    if _SOURCE_MAP is not None:
        return _SOURCE_MAP
    try:
        raw = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("title_map", {})
    raw.setdefault("updated_at", "")
    _SOURCE_MAP = raw
    return _SOURCE_MAP


def _save_source_map() -> None:
    data = _load_source_map()
    data["updated_at"] = _now_iso()
    try:
        MAP_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _legacy_summary(title_id: str) -> dict[str, Any]:
    return dict(_load_title_summaries().get(_clean(title_id)) or {})


def get_cached_title_summary(title_id: str) -> dict[str, Any] | None:
    title_id = _clean(title_id)
    cached = _cache_get(f"summary:{title_id}", TITLE_TTL)
    if isinstance(cached, dict):
        return dict(cached)
    summary = _legacy_summary(title_id)
    return summary or None


def _score(query: str, title: str) -> tuple[int, int]:
    q = _normalize_text(query)
    t = _normalize_text(title)
    if not q or not t:
        return (0, -len(t))
    if t == q:
        return (500, -len(t))
    if t.startswith(q):
        return (400, -len(t))
    if q in t:
        return (300, -len(t))
    overlap = len(set(q.split()) & set(t.split()))
    return (100 + overlap * 10, -len(t))


def _dedupe_texts(*groups: Any) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if not group:
            continue
        values = [group] if isinstance(group, str) else list(group)
        for value in values:
            if isinstance(value, dict):
                value = value.get("name") or value.get("label") or value.get("title") or value.get("value") or ""
            text = _clean(value)
            key = _normalize_text(text)
            if text and key and key not in seen:
                seen.add(key)
                output.append(text)
    return output


def _mangabaka_headers() -> dict[str, str]:
    return {
        **_headers(f"{MANGABAKA_SITE_BASE}/"),
        "Accept": "application/json,*/*",
        "Origin": MANGABAKA_SITE_BASE,
    }


async def _request_mangabaka_json(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(headers=_mangabaka_headers(), follow_redirects=True, timeout=MANGABAKA_TIMEOUT) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


def _mangabaka_record_titles(record: dict[str, Any]) -> list[str]:
    titles: list[Any] = [
        record.get("title"),
        record.get("native_title"),
        record.get("romanized_title"),
    ]
    secondary = record.get("secondary_titles") or []
    if isinstance(secondary, list):
        titles.extend(secondary)
    for item in record.get("titles") or []:
        if isinstance(item, dict):
            titles.append(item.get("title"))
        else:
            titles.append(item)
    return _dedupe_texts(titles)


def _mangabaka_match_score(query: str, record: dict[str, Any]) -> int:
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return 0
    best = 0
    query_words = set(normalized_query.split())
    for title in _mangabaka_record_titles(record):
        normalized_title = _normalize_text(title)
        if not normalized_title:
            continue
        if normalized_title == normalized_query:
            best = max(best, 700)
        elif normalized_title.startswith(normalized_query) or normalized_query.startswith(normalized_title):
            best = max(best, 520)
        elif normalized_query in normalized_title or normalized_title in normalized_query:
            best = max(best, 420)
        else:
            title_words = set(normalized_title.split())
            overlap = len(query_words & title_words)
            if overlap:
                best = max(best, 100 + overlap * 60)
    return best


def _mangabaka_cover_url(record: dict[str, Any]) -> str:
    cover = record.get("cover") or {}
    if not isinstance(cover, dict):
        return ""
    for size in ("x350", "x250", "x150"):
        sized = cover.get(size) or {}
        if not isinstance(sized, dict):
            continue
        for density in ("x3", "x2", "x1", "url"):
            if sized.get(density):
                return _clean(sized.get(density))
    raw = cover.get("raw") or {}
    if isinstance(raw, dict) and raw.get("url"):
        return _clean(raw.get("url"))
    return ""


async def _find_mangabaka_record(title: str, *, network: bool = True) -> dict[str, Any] | None:
    title = _clean(title)
    normalized = _normalize_text(title)
    if not normalized:
        return None
    key = f"mangabaka:search:{normalized}"
    cached = _cache_get(key, MANGABAKA_TTL)
    if cached is not None:
        return dict(cached) if isinstance(cached, dict) else None
    if not network:
        return None

    async def _load():
        url = f"{MANGABAKA_API_BASE}/series/search?q={quote(title)}&limit=5"
        try:
            data = await _request_mangabaka_json(url)
        except Exception:
            return None
        rows = data.get("data") if isinstance(data, dict) else []
        if not isinstance(rows, list):
            return None
        scored = [(_mangabaka_match_score(title, row), row) for row in rows if isinstance(row, dict)]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if not scored or scored[0][0] < 220:
            return None
        return scored[0][1]

    return await _dedup(key, MANGABAKA_TTL, _load)


async def _enrich_with_mangabaka(item: dict[str, Any], *, network: bool = True) -> dict[str, Any]:
    if not isinstance(item, dict):
        return item
    title = item.get("title") or item.get("display_title") or item.get("preferred_title") or ""
    record = await _find_mangabaka_record(title, network=network)
    if not record:
        return item

    enriched = dict(item)
    cover = _mangabaka_cover_url(record)
    mangabaka_genres = _dedupe_texts(record.get("genres") or [])
    genres = mangabaka_genres or _dedupe_texts(enriched.get("genres") or [], enriched.get("anilist_genres") or [])
    tags = _dedupe_texts(enriched.get("tags") or [], record.get("tags") or [], record.get("tags_v2") or [])
    mangabaka_id = _clean(record.get("id"))

    if mangabaka_id:
        enriched["mangabaka_id"] = mangabaka_id
        enriched["mangabaka_url"] = f"{MANGABAKA_SITE_BASE}/{mangabaka_id}"
    if cover:
        enriched["mangabaka_cover_url"] = cover
        enriched["origin_cover_url"] = cover
        enriched["cover_url"] = cover
        enriched["background_url"] = cover
        enriched["banner_url"] = cover
    if genres:
        enriched["genres"] = genres
    if tags:
        enriched["tags"] = tags
        enriched["mangabaka_tags"] = tags
    if not enriched.get("description") and record.get("description"):
        enriched["description"] = _clean(record.get("description"))
    if not enriched.get("authors"):
        enriched["authors"] = _dedupe_texts(record.get("authors") or [])
    if record.get("year") and not enriched.get("published"):
        enriched["published"] = str(record.get("year"))
    if record.get("total_chapters") and not enriched.get("source_total_chapters"):
        enriched["source_total_chapters"] = record.get("total_chapters")
    enriched["metadata_source"] = "mangabaka+mangafire"
    _remember_title_summary(enriched)
    return enriched


async def _enrich_items_with_mangabaka(
    items: list[dict[str, Any]],
    *,
    limit: int | None = None,
    network: bool = True,
    cap_network: bool = True,
) -> list[dict[str, Any]]:
    if not items:
        return items
    max_items = len(items) if limit is None else min(len(items), max(0, int(limit)))
    if network and cap_network:
        max_items = min(max_items, max(0, MANGABAKA_BATCH_NETWORK_LIMIT))
    if max_items <= 0:
        return items
    enriched = await asyncio.gather(*(_enrich_with_mangabaka(item, network=network) for item in items[:max_items]))
    return [*enriched, *items[max_items:]]


def _schedule_mangabaka_enrich(items: list[dict[str, Any]], *, limit: int | None = None) -> None:
    if not items:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_enrich_items_with_mangabaka(items, limit=limit or MANGABAKA_BATCH_NETWORK_LIMIT, network=True))


def _title_from_anchor(anchor) -> dict[str, Any] | None:
    href = _absolute(anchor.get("href") or "")
    hid = _hid_from_title_id(href)
    info = anchor.select_one(".info h6, .info a, h6")
    title = _clean(anchor.get("title") or (info.text if info else "") or anchor.text)
    if not hid or not title:
        title = _clean(info.text if info else title)
    img = anchor.select_one("img")
    cover = _absolute((img.get("src") or img.get("data-src") or img.get("data-original") or img.get("data-lazy-src")) if img else "")
    if not hid or not title:
        return None
    item = {
        "title_id": _mf_title_id(hid),
        "source_title_id": _mf_title_id(hid),
        "mangafire_id": _mf_title_id(hid),
        "hid": hid,
        "title": title,
        "display_title": title,
        "url": href,
        "title_url": href,
        "cover_url": cover,
        "background_url": cover,
        "status": "",
        "rating": "",
        "source": "mangafire",
    }
    return item


def _parse_search_items(html_text: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    selectors = [
        ".original.card-lg .unit .inner",
        "main .unit .inner",
        "main a.unit",
        "a[href*='/manga/']",
    ]
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for selector in selectors:
        for element in soup.select(selector):
            anchor = element if element.name == "a" else (
                element.select_one(".info a[href*='/manga/']")
                or element.select_one("a[href*='/manga/']:not(.poster)")
                or element.select_one("a[href*='/manga/']")
            )
            if not anchor:
                continue
            item = _title_from_anchor(anchor)
            if not item:
                continue
            if not item.get("cover_url"):
                img = element.select_one(".poster img, img")
                cover = _absolute((img.get("src") or img.get("data-src") or img.get("data-original") or img.get("data-lazy-src")) if img else "")
                if cover:
                    item["cover_url"] = cover
                    item["background_url"] = cover
            if item["title_id"] in seen:
                continue
            seen.add(item["title_id"])
            items.append(item)
        if items:
            break
    return items


def _parse_latest_from_unit(element) -> dict[str, str]:
    links = element.select("a[href*='/read/']")
    link = None
    for candidate in links:
        parsed_candidate = _parse_chapter_id(candidate.get("href") or "")
        if _norm_lang(parsed_candidate.get("lang")) == "pt-br":
            link = candidate
            break
    if link is None and links:
        link = links[0]
    if not link:
        return {}
    parsed = _parse_chapter_id(link.get("href") or "")
    if not parsed:
        return {}
    text = _clean(link.text or link.get("title") or "")
    number = parsed.get("number") or re.sub(r"[^0-9.]+", "", text)
    return {
        "chapter_id": _chapter_id(parsed["hid"], parsed["lang"], parsed["kind"], number),
        "chapter_url": _absolute(link.get("href") or ""),
        "chapter_number": number,
        "chapter_language": _norm_lang(parsed["lang"]),
        "language": _norm_lang(parsed["lang"]),
    }


def _parse_listing_titles(html_text: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    items = _parse_search_items(html_text)
    if not items:
        return []
    by_id = {item["title_id"]: item for item in items}
    for unit in soup.select(".unit, .item, article"):
        anchor = unit.select_one("a[href*='/manga/']")
        if not anchor:
            continue
        hid = _hid_from_title_id(anchor.get("href") or "")
        item = by_id.get(_mf_title_id(hid))
        if not item:
            continue
        latest = _parse_latest_from_unit(unit)
        if latest:
            item.update(latest)
            item["latest_chapter"] = latest.get("chapter_number") or ""
    return list(by_id.values())


async def _search_vrf(query: str) -> str:
    query = _clean(query)
    cached = _VRF_CACHE.get(query.lower())
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    vrf = _generate_vrf(query)
    _VRF_CACHE[query.lower()] = (time.time(), vrf)
    return vrf
    try:
        from playwright.async_api import async_playwright
    except Exception as error:
        raise RuntimeError("Playwright indisponivel para gerar token de busca MangaFire.") from error

    captured = ""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1366,768",
            ],
        )
        page = None
        try:
            page = await browser.new_page(user_agent=BROWSER_USER_AGENT)
            await page.add_init_script(
                """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
const __mf_noop = function(){};
['clear','debug','log','warn','error','table'].forEach((key) => {
  try { console[key] = __mf_noop; } catch (error) {}
});
try { window.open = __mf_noop; } catch (error) {}
"""
            )

            def on_request(request):
                nonlocal captured
                url = request.url
                if "mangafire.to/ajax/manga/search" in url and "vrf=" in url:
                    captured = url

            page.on("request", on_request)

            await page.goto(f"{BASE_URL}/home", wait_until="domcontentloaded", timeout=30000)
            for _ in range(30):
                try:
                    await page.fill("input[name=keyword]", query)
                    await page.dispatch_event("input[name=keyword]", "keyup")
                except Exception:
                    pass
                if captured:
                    break
                await page.wait_for_timeout(250)
        finally:
            if page is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(page.close(), timeout=5)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(browser.close(), timeout=5)
    match = re.search(r"[?&]vrf=([^&]+)", captured)
    if not match:
        raise RuntimeError("Nao consegui gerar vrf de busca MangaFire.")
    vrf = match.group(1)
    _VRF_CACHE[query.lower()] = (time.time(), vrf)
    return vrf


async def _search_mangafire_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    query = _clean(query)
    limit = max(1, int(limit or SEARCH_LIMIT))
    if not query:
        return []
    vrf = await _search_vrf(query)
    ajax_url = f"{BASE_URL}/ajax/manga/search?keyword={quote(query)}&vrf={quote(vrf)}"
    try:
        data = await _request_json(ajax_url, referer=f"{BASE_URL}/home", timeout=12.0)
        result = data.get("result") or {}
        html_text = result.get("html") if isinstance(result, dict) else ""
    except Exception:
        url = f"{BASE_URL}/filter?keyword={quote(query)}&language%5B%5D=pt-br&page=1&vrf={quote(vrf)}"
        try:
            html_text = await _request_text(url, timeout=12.0)
        except Exception:
            return get_search_fallback_titles(query, limit)
    items = _parse_search_items(html_text)
    items.sort(key=lambda item: _score(query, item.get("title") or ""), reverse=True)
    return items[:limit]


async def search_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    query = _clean(query)
    limit = max(1, int(limit or SEARCH_LIMIT))
    if not query:
        return []

    async def _load():
        items = await _search_mangafire_titles(query, limit)
        return await _enrich_items_with_mangabaka(items, limit=min(limit, 3), network=True)

    return await _dedup(f"search:{query}:{limit}", SEARCH_TTL, _load)


async def search_titles_fast(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    query = _clean(query)
    limit = max(1, int(limit or SEARCH_LIMIT))
    if not query:
        return []

    async def _load():
        items = await _search_mangafire_titles(query, limit)
        return await _enrich_items_with_mangabaka(items, limit=limit, network=False)

    return await _dedup(f"search-fast:{query}:{limit}", SEARCH_TTL, _load)


def get_cached_search_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]] | None:
    cached = _cache_get(f"search:{_clean(query)}:{max(1, int(limit or SEARCH_LIMIT))}", SEARCH_TTL)
    return list(cached) if isinstance(cached, list) else None


def get_search_fallback_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    q = _normalize_text(query)
    candidates = []
    for title_id, item in _load_title_summaries().items():
        title = item.get("title") or item.get("display_title") or ""
        if q and q not in _normalize_text(title):
            continue
        candidates.append({**item, "title_id": title_id})
    candidates.sort(key=lambda item: _score(query, item.get("title") or ""), reverse=True)
    return candidates[: max(1, int(limit or SEARCH_LIMIT))]


async def _resolve_title_ref(title_ref: str) -> dict[str, str]:
    raw = _clean(title_ref)
    hid = _hid_from_title_id(raw)
    if hid:
        return {"public_id": _mf_title_id(hid), "source_id": _mf_title_id(hid), "hid": hid}

    source_map = _load_source_map()
    mapped = source_map.get("title_map", {}).get(raw)
    if isinstance(mapped, dict):
        source_id = _clean(mapped.get("mangafire_id") or mapped.get("source_id"))
        hid = _hid_from_title_id(source_id)
        if hid:
            return {"public_id": raw, "source_id": _mf_title_id(hid), "hid": hid}

    summary = _legacy_summary(raw)
    title = summary.get("title") or summary.get("display_title") or summary.get("preferred_title") or ""
    if title:
        results = await search_titles(title, limit=6)
        if results:
            normalized = _normalize_text(title)
            best = next((item for item in results if _normalize_text(item.get("title")) == normalized), results[0])
            source_id = best["title_id"]
            hid = _hid_from_title_id(source_id)
            source_map.setdefault("title_map", {})[raw] = {
                "mangafire_id": source_id,
                "source_id": source_id,
                "title": best.get("title") or title,
                "legacy_title": title,
                "mapped_at": _now_iso(),
            }
            _save_source_map()
            return {"public_id": raw, "source_id": source_id, "hid": hid}

    if LEGACY_TITLE_RE.match(raw):
        raise RuntimeError(f"Nao consegui mapear a obra antiga {raw} para o MangaFire ainda.")
    raise ValueError("Referencia de obra invalida.")


def _extract_sync_data(soup: BeautifulSoup) -> dict[str, Any]:
    node = soup.select_one("#syncData")
    if not node:
        return {}
    try:
        return json.loads(node.text or "{}")
    except Exception:
        return {}


def _parse_detail_html(html_text: str, url: str, public_id: str | None = None) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")
    sync = _extract_sync_data(soup)
    canonical = _absolute((soup.select_one("link[rel=canonical]") or {}).get("href") if soup.select_one("link[rel=canonical]") else url)
    hid = _hid_from_title_id(canonical) or _hid_from_title_id(url) or _hid_from_title_id(soup.select_one("#manga-page", attrs={"data-id": True}).get("data-id") if soup.select_one("#manga-page", attrs={"data-id": True}) else "")
    title = _clean((soup.select_one(".main-inner h1") or soup.select_one("h1") or {}).text if (soup.select_one(".main-inner h1") or soup.select_one("h1")) else sync.get("name"))
    cover = _absolute((soup.select_one(".poster img") or soup.select_one("meta[property='og:image']") or {}).get("src") or (soup.select_one("meta[property='og:image']") or {}).get("content") if (soup.select_one(".poster img") or soup.select_one("meta[property='og:image']")) else "")
    description = _clean((soup.select_one("#synopsis .modal-content") or soup.select_one("meta[name='description']") or {}).text if soup.select_one("#synopsis .modal-content") else (soup.select_one("meta[name='description']") or {}).get("content", ""))

    meta_text = soup.get_text("\n")
    def after(label: str) -> str:
        match = re.search(rf"{re.escape(label)}:\s*\n?\s*([^\n]+)", meta_text, flags=re.I)
        return _clean(match.group(1)) if match else ""

    genres = [a.get_text(strip=True) for a in soup.select("a[href*='/genre/']") if a.get_text(strip=True)]
    authors = [a.get_text(strip=True) for a in soup.select("a[href*='/author/']") if a.get_text(strip=True)]
    languages = []
    for node in soup.select("[data-code]"):
        code = _norm_lang(node.get("data-code"))
        label = _clean(node.get_text(" ", strip=True))
        if code and code not in [item["code"] for item in languages]:
            languages.append({"code": code, "label": label or code.upper()})

    item = {
        "title_id": public_id or _mf_title_id(hid),
        "source_title_id": _mf_title_id(hid),
        "mangafire_id": _mf_title_id(hid),
        "hid": hid,
        "title": title or "Manga",
        "display_title": title or "Manga",
        "preferred_title": title or "",
        "alt_titles": [],
        "url": canonical,
        "title_url": canonical,
        "cover_url": cover,
        "background_url": cover,
        "description": description,
        "status": after("Status") or _clean((soup.select_one(".info > p") or {}).text if soup.select_one(".info > p") else ""),
        "rating": after("MAL") or after("Score"),
        "genres": genres,
        "authors": authors,
        "published": after("Published"),
        "languages": languages,
        "source": "mangafire",
        "source_total_chapters": 0,
    }
    _remember_title_summary(item)
    return item


async def get_title_details(title_ref: str) -> dict[str, Any]:
    resolved = await _resolve_title_ref(title_ref)
    async def _load():
        url = f"{BASE_URL}/manga/{resolved['hid']}"
        # canonical slug is not required; /manga/{hid} redirects poorly on some titles, so use cached URL first.
        mapped = _load_source_map().get("title_map", {}).get(resolved["public_id"], {})
        cached_url = mapped.get("url") if isinstance(mapped, dict) else ""
        summary = get_cached_title_summary(resolved["source_id"]) or get_cached_title_summary(resolved["public_id"]) or {}
        url = cached_url or summary.get("url") or summary.get("title_url") or url
        html_text = await _request_text(_absolute(url))
        details = _parse_detail_html(html_text, _absolute(url), public_id=resolved["public_id"])
        if details.get("hid"):
            _load_source_map().setdefault("title_map", {})[resolved["public_id"]] = {
                "mangafire_id": details["source_title_id"],
                "source_id": details["source_title_id"],
                "title": details["title"],
                "url": details["url"],
                "mapped_at": _now_iso(),
            }
            _save_source_map()
        return await _enrich_with_mangabaka(details)
    return await _dedup(f"details:{resolved['public_id']}", TITLE_TTL, _load)


async def get_title_overview(title_ref: str) -> dict[str, Any]:
    details = await get_title_details(title_ref)
    anilist = await enrich_title_metadata(details.get("title") or "", details.get("alt_titles") or [])
    merged = _merge_metadata(details, anilist)
    merged = await _enrich_with_mangabaka(merged)
    _remember_title_summary(merged)
    return merged


def _parse_chapter_list(result_html: str, hid: str, lang: str, public_id: str, kind: str = "chapter") -> list[dict[str, Any]]:
    soup = BeautifulSoup(result_html or "", "html.parser")
    nodes = soup.select(".item, li")
    chapters = []
    for node in nodes:
        link = node.select_one("a[href*='/read/']")
        if not link:
            continue
        parsed = _parse_chapter_id(link.get("href") or "")
        number = _clean(node.get("data-number") or parsed.get("number"))
        if not number:
            continue
        span = node.select_one("span")
        name = _clean(span.text if span else link.get("title") or f"Chapter {number}")
        date_text = _clean((node.select("span")[1].text if len(node.select("span")) > 1 else ""))
        chapter_lang = _norm_lang(parsed.get("lang") or lang)
        chapter_kind = parsed.get("kind") or kind
        cid = _chapter_id(hid, chapter_lang, chapter_kind, number)
        url = _absolute(link.get("href") or "")
        source_chapter_id = _clean(link.get("data-id") or node.get("data-id"))
        translation = {
            "id": cid,
            "source_chapter_id": source_chapter_id,
            "url": url,
            "language": chapter_lang,
            "volume": "",
            "group_name": "MangaFire",
            "date": date_text,
        }
        chapters.append(
            {
                "chapter_id": cid,
                "source_chapter_id": source_chapter_id,
                "chapter_url": url,
                "chapter_number": number,
                "chapter_number_float": number,
                "chapter_language": chapter_lang,
                "chapter_volume": "",
                "group_name": "MangaFire",
                "title": name,
                "translations": [translation],
            }
        )
        _CHAPTER_TITLE_CACHE[cid] = public_id
        _CHAPTER_URL_CACHE[cid] = url
        if source_chapter_id:
            _CHAPTER_SOURCE_ID_CACHE[cid] = source_chapter_id
    return chapters


def _parse_volume_cover_map(result_html: str) -> dict[str, str]:
    soup = BeautifulSoup(result_html or "", "html.parser")
    covers: dict[str, str] = {}
    for node in soup.select(".unit.item, .item"):
        number = _clean(node.get("data-number"))
        img = node.select_one("img")
        cover = _absolute((img or {}).get("data-src") or (img or {}).get("src") or "")
        if not number or not cover or "no-image" in cover:
            continue
        covers[number] = cover
    return covers


async def get_chapter_list(title_id: str, lang: str | None = None) -> dict[str, Any]:
    resolved_lang = _norm_lang(lang or PREFERRED_CHAPTER_LANG)
    resolved = await _resolve_title_ref(title_id)

    async def _load():
        default_languages = [
            {"code": "pt-br", "label": "Portuguese (Br)"},
            {"code": "en", "label": "English"},
            {"code": "es-la", "label": "Spanish (LATAM)"},
            {"code": "fr", "label": "French"},
            {"code": "ja", "label": "Japanese"},
        ]
        languages = (get_cached_title_summary(resolved["public_id"]) or {}).get("languages") or default_languages

        def _fallback_languages() -> list[str]:
            seen = {resolved_lang}
            codes: list[str] = []
            for item in languages:
                code = _norm_lang((item or {}).get("code") if isinstance(item, dict) else item)
                if code and code not in seen:
                    codes.append(code)
                    seen.add(code)
            for code in ("en", "es-la", "es", "pt", "fr", "ja"):
                normalized = _norm_lang(code)
                if normalized not in seen:
                    codes.append(normalized)
                    seen.add(normalized)
            return codes

        async def _load_kind(kind: str, item_lang: str) -> list[dict[str, Any]]:
            vrf = _generate_vrf(f"{resolved['hid']}@{kind}@{item_lang}")
            url = f"{BASE_URL}/ajax/read/{resolved['hid']}/{kind}/{item_lang}?vrf={quote(vrf)}"
            data = await _request_json(url, referer=f"{BASE_URL}/manga/{resolved['hid']}")
            result = data.get("result") or {}
            html_text = result.get("html") if isinstance(result, dict) else result
            return _parse_chapter_list(html_text or "", resolved["hid"], item_lang, resolved["public_id"], kind)

        async def _apply_volume_covers(items: list[dict[str, Any]], item_lang: str) -> list[dict[str, Any]]:
            if not items:
                return items
            try:
                data = await _request_json(
                    f"{BASE_URL}/ajax/manga/{resolved['hid']}/volume/{item_lang}",
                    referer=f"{BASE_URL}/manga/{resolved['hid']}",
                    timeout=12.0,
                )
                cover_map = _parse_volume_cover_map(data.get("result") or "")
            except Exception:
                cover_map = {}
            if not cover_map:
                return items
            for item in items:
                cover = cover_map.get(_clean(item.get("chapter_number")))
                if cover:
                    item["cover_url"] = cover
                    for tr in item.get("translations") or []:
                        tr["cover_url"] = cover
            return items

        chapters = await _load_kind("chapter", resolved_lang)
        try:
            volumes = await _load_kind("volume", resolved_lang)
            volumes = await _apply_volume_covers(volumes, resolved_lang)
        except Exception:
            volumes = []
        if not chapters:
            data = await _request_json(f"{BASE_URL}/ajax/manga/{resolved['hid']}/chapter/{resolved_lang}", referer=f"{BASE_URL}/manga/{resolved['hid']}")
            chapters = _parse_chapter_list(data.get("result") or "", resolved["hid"], resolved_lang, resolved["public_id"], "chapter")
        if not chapters:
            for fallback_lang in _fallback_languages():
                try:
                    chapters = await _load_kind("chapter", fallback_lang)
                    try:
                        volumes = await _load_kind("volume", fallback_lang)
                        volumes = await _apply_volume_covers(volumes, fallback_lang)
                    except Exception:
                        volumes = []
                except Exception:
                    chapters = []
                    volumes = []
                if chapters:
                    break
        payload = {
            "title_id": resolved["public_id"],
            "source_title_id": resolved["source_id"],
            "chapters": chapters,
            "volumes": volumes,
            "languages": languages,
            "total_translations": len(chapters),
            "total_volumes": len(volumes),
            "partial": False,
            "source": "mangafire",
        }
        return payload

    return await _dedup(f"chapters:{resolved['public_id']}:{resolved_lang}", CHAPTERS_TTL, _load)


async def get_chapter_list_fast(title_id: str, lang: str | None = None) -> dict[str, Any]:
    return await get_chapter_list(title_id, lang)


def get_cached_chapter_list(title_id: str, lang: str | None = None) -> dict[str, Any] | None:
    resolved_lang = _norm_lang(lang or PREFERRED_CHAPTER_LANG)
    cached = _cache_get(f"chapters:{_clean(title_id)}:{resolved_lang}", CHAPTERS_TTL)
    return dict(cached) if isinstance(cached, dict) else None


def flatten_chapters(chapter_payload: dict[str, Any] | list[Any], preferred_lang: str | None = None, *, ascending: bool = False) -> list[dict[str, Any]]:
    if isinstance(chapter_payload, list):
        chapter_payload = {"chapters": chapter_payload}
    if not isinstance(chapter_payload, dict):
        return []
    title_id = _clean(chapter_payload.get("title_id"))
    chapters = list(chapter_payload.get("chapters") or [])
    def num(ch):
        try:
            return float(_clean(ch.get("chapter_number")))
        except Exception:
            return -1.0
    chapters.sort(key=num, reverse=not ascending)
    items = []
    for chapter in chapters:
        translations = chapter.get("translations") or []
        if not translations:
            continue
        tr = translations[0]
        items.append(
            {
                "chapter_id": tr.get("id") or "",
                "chapter_url": tr.get("url") or "",
                "title_id": title_id,
                "chapter_number": chapter.get("chapter_number") or "",
                "chapter_number_float": chapter.get("chapter_number_float") or chapter.get("chapter_number") or "",
                "chapter_language": tr.get("language") or preferred_lang or PREFERRED_CHAPTER_LANG,
                "chapter_volume": tr.get("volume") or "",
                "group_name": tr.get("group_name") or "MangaFire",
                "updated_at": tr.get("date") or "",
                "title": chapter.get("title") or "",
                "cover_url": tr.get("cover_url") or chapter.get("cover_url") or "",
            }
        )
    return items


def get_adjacent_chapters(chapter_payload: dict[str, Any], chapter_id: str, preferred_lang: str | None = None):
    flattened = flatten_chapters(chapter_payload, preferred_lang, ascending=True)
    current = _clean(chapter_id)
    for index, item in enumerate(flattened):
        if item.get("chapter_id") == current:
            return (flattened[index - 1] if index > 0 else None, flattened[index + 1] if index + 1 < len(flattened) else None)
    return None, None


def _merge_metadata(details: dict[str, Any], anilist: dict[str, Any]) -> dict[str, Any]:
    merged = dict(details)
    if not anilist:
        return merged
    genres = []
    seen = set()
    for raw in [*(details.get("genres") or []), *(anilist.get("anilist_genres") or [])]:
        text = _clean(raw)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            genres.append(text)
    merged["genres"] = genres
    for key in (
        "anilist_id", "anilist_url", "anilist_status", "anilist_format", "anilist_score",
        "anilist_chapters", "anilist_volumes", "anilist_country", "anilist_titles",
        "cover_color", "banner_url",
    ):
        merged[key] = anilist.get(key) or merged.get(key) or (0 if key.endswith(("score", "chapters", "volumes")) else "")
    if not merged.get("cover_url") and anilist.get("cover_url_anilist"):
        merged["cover_url"] = anilist["cover_url_anilist"]
    if not merged.get("background_url"):
        merged["background_url"] = merged.get("banner_url") or merged.get("cover_url") or ""
    if not merged.get("description") and anilist.get("anilist_description"):
        merged["description"] = anilist["anilist_description"]
    return merged


async def get_title_chapters_snapshot(title_ref: str, lang: str | None = None) -> dict[str, Any]:
    resolved = await _resolve_title_ref(title_ref)
    summary = get_cached_title_summary(resolved["public_id"]) or {}
    chapters_payload = await get_chapter_list(resolved["public_id"], lang)
    chapters = chapters_payload.get("chapters") or []
    volumes = chapters_payload.get("volumes") or []
    latest = flatten_chapters(chapters_payload, lang)
    bundle = {
        "title_id": resolved["public_id"],
        "source_title_id": resolved["source_id"],
        "title": summary.get("title") or summary.get("display_title") or "Manga",
        "display_title": summary.get("display_title") or summary.get("title") or "Manga",
        "cover_url": summary.get("cover_url") or "",
        "background_url": summary.get("background_url") or summary.get("cover_url") or "",
        "status": summary.get("status") or "carregando",
        "rating": summary.get("rating") or "",
        "genres": summary.get("genres") or [],
        "chapters": chapters,
        "volumes": volumes,
        "languages": chapters_payload.get("languages") or [],
        "total_chapters": len(chapters),
        "total_volumes": len(volumes),
        "source_total_chapters": len(chapters),
        "latest_chapter": latest[0] if latest else None,
        "chapters_partial": False,
        "metadata_partial": True,
        "source": "mangafire",
    }
    bundle = await _enrich_with_mangabaka(bundle)
    _remember_title_summary(bundle)
    return bundle


async def get_title_bundle(title_ref: str, lang: str | None = None) -> dict[str, Any]:
    resolved = await _resolve_title_ref(title_ref)
    resolved_lang = _norm_lang(lang or PREFERRED_CHAPTER_LANG)

    async def _load():
        details, chapters_payload = await asyncio.gather(
            get_title_details(resolved["public_id"]),
            get_chapter_list(resolved["public_id"], resolved_lang),
        )
        anilist = await enrich_title_metadata(details.get("title") or "", details.get("alt_titles") or [])
        merged = _merge_metadata(details, anilist)
        merged["chapters"] = chapters_payload.get("chapters") or []
        merged["volumes"] = chapters_payload.get("volumes") or []
        merged["languages"] = chapters_payload.get("languages") or merged.get("languages") or []
        merged["total_chapters"] = len(merged["chapters"])
        merged["total_volumes"] = len(merged["volumes"])
        merged["source_total_chapters"] = len(merged["chapters"])
        merged["chapters_partial"] = False
        merged["metadata_partial"] = False
        latest = flatten_chapters(chapters_payload, resolved_lang)
        merged["latest_chapter"] = latest[0] if latest else None
        merged = await _enrich_with_mangabaka(merged)
        _remember_title_summary(merged)
        return merged

    return await _dedup(f"bundle:{resolved['public_id']}:{resolved_lang}", TITLE_TTL, _load)


async def get_chapter_details(chapter_ref: str) -> dict[str, Any]:
    parsed = _parse_chapter_id(chapter_ref)
    if not parsed:
        raise ValueError("Referencia de capitulo MangaFire invalida.")
    cid = _chapter_id(parsed["hid"], parsed["lang"], parsed["kind"], parsed["number"])
    title_id = _CHAPTER_TITLE_CACHE.get(cid) or _mf_title_id(parsed["hid"])
    url = _clean(chapter_ref) if _clean(chapter_ref).startswith("http") else f"{BASE_URL}/read/{parsed['hid']}/{parsed['lang']}/{parsed['kind']}-{parsed['number']}"
    html_text = await _request_text(url, referer=f"{BASE_URL}/manga/{parsed['hid']}")
    soup = BeautifulSoup(html_text, "html.parser")
    body = soup.select_one("body")
    canonical = _absolute((soup.select_one("link[rel=canonical]") or {}).get("href") if soup.select_one("link[rel=canonical]") else url)
    canonical_parsed = _parse_chapter_id(canonical) or parsed
    number = canonical_parsed.get("number") or parsed["number"]
    cid = _chapter_id(parsed["hid"], canonical_parsed.get("lang") or parsed["lang"], canonical_parsed.get("kind") or parsed["kind"], number)
    title = _clean((soup.select_one("#ctrl-menu .head a") or soup.select_one("meta[property='og:title']") or {}).text if soup.select_one("#ctrl-menu .head a") else (soup.select_one("meta[property='og:title']") or {}).get("content", ""))
    return {
        "title_id": title_id,
        "title": title.replace(" Manga, Chapter " + str(number), "").strip(" |") or "Manga",
        "chapter_id": cid,
        "chapter_number": number,
        "chapter_language": canonical_parsed.get("lang") or parsed["lang"],
        "chapter_volume": "",
        "chapter_url": canonical,
        "cover_url": "",
        "images": [],
        "image_count": 0,
        "source": "mangafire",
    }


async def _capture_reader_ajax(chapter_url: str) -> str:
    try:
        from playwright.async_api import async_playwright
    except Exception as error:
        raise RuntimeError("Playwright indisponivel para carregar paginas MangaFire.") from error

    captured = ""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1366,768",
            ],
        )
        page = None
        try:
            page = await browser.new_page(user_agent=BROWSER_USER_AGENT)
            await page.add_init_script(
                """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
const __mf_noop = function(){};
['clear','debug','log','warn','error','table'].forEach((key) => {
  try { console[key] = __mf_noop; } catch (error) {}
});
try { window.open = __mf_noop; } catch (error) {}
"""
            )

            def on_request(request):
                nonlocal captured
                url = request.url
                path = urlparse(url).path
                if "mangafire.to" in url and re.search(r"/ajax/read/(chapter|volume)/", path) and "vrf=" in url:
                    captured = url

            page.on("request", on_request)

            async def route_handler(route):
                nonlocal captured
                url = route.request.url
                host = urlparse(url).netloc
                path = urlparse(url).path
                if url == chapter_url:
                    await route.continue_()
                    return
                if "mfcdn.nl" in host and (path.endswith(".js") or "/js/" in path or path.endswith(".css")):
                    await route.continue_()
                    return
                if "cloudflare.com" in host and ("jquery" in path or path.endswith(".js")):
                    await route.continue_()
                    return
                if "cdnjs.cloudflare.com" in host and (path.endswith(".js") or path.endswith(".css")):
                    await route.continue_()
                    return
                if "mangafire.to" in host and "/ajax/read" in path:
                    if re.search(r"/ajax/read/(chapter|volume)/", path) and "vrf=" in url:
                        captured = url
                        await route.abort()
                        return
                    await route.continue_()
                    return
                await route.abort()

            await page.route("**/*", route_handler)
            await page.goto(chapter_url, wait_until="domcontentloaded", timeout=30000)
            for _ in range(80):
                if captured:
                    break
                await page.wait_for_timeout(250)
        finally:
            if page is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(page.close(), timeout=5)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(browser.close(), timeout=5)
    if not captured:
        raise RuntimeError("Nao consegui capturar endpoint do leitor MangaFire.")
    return captured


async def get_chapter_reader_payload(chapter_ref: str, lang: str | None = None, title_hint: str = "") -> dict[str, Any]:
    parsed = _parse_chapter_id(chapter_ref)
    if not parsed:
        raise ValueError("Referencia de capitulo invalida.")
    cid = _chapter_id(parsed["hid"], parsed["lang"], parsed["kind"], parsed["number"])
    resolved_lang = _norm_lang(lang or parsed["lang"])

    def _reader_title_ref(cached_id: str = "") -> str:
        hinted = _clean(title_hint)
        if hinted and (_hid_from_title_id(hinted) or LEGACY_TITLE_RE.match(hinted)):
            return hinted
        return cached_id or _CHAPTER_TITLE_CACHE.get(cid) or _mf_title_id(parsed["hid"])

    async def _load():
        chapter_url = _CHAPTER_URL_CACHE.get(cid) or _clean(chapter_ref)
        source_chapter_id = _CHAPTER_SOURCE_ID_CACHE.get(cid)
        if not chapter_url.startswith("http") or not source_chapter_id:
            title_id = _reader_title_ref()
            try:
                await get_chapter_list(title_id, parsed["lang"])
            except Exception:
                pass
            chapter_url = _CHAPTER_URL_CACHE.get(cid) or chapter_url
            source_chapter_id = _CHAPTER_SOURCE_ID_CACHE.get(cid) or source_chapter_id
        details = await get_chapter_details(chapter_url)
        if source_chapter_id:
            vrf = _generate_vrf(f"{parsed['kind']}@{source_chapter_id}")
            ajax_url = f"{BASE_URL}/ajax/read/{parsed['kind']}/{source_chapter_id}?vrf={quote(vrf)}"
        else:
            ajax_url = await _capture_reader_ajax(details["chapter_url"] or chapter_url)
        data = await _request_json(ajax_url, referer=details["chapter_url"] or chapter_url)
        images_raw = ((data.get("result") or {}).get("images") or [])
        images = []
        image_offsets = []
        for row in images_raw:
            if isinstance(row, list) and row:
                images.append(_absolute(row[0]))
                try:
                    image_offsets.append(int(row[2]))
                except Exception:
                    image_offsets.append(0)
            elif isinstance(row, str):
                images.append(_absolute(row))
                image_offsets.append(0)
        title_id = _reader_title_ref(_CHAPTER_TITLE_CACHE.get(details["chapter_id"]) or details.get("title_id") or "")
        chapters_payload = await get_chapter_list(title_id, resolved_lang)
        navigation_payload = (
            {"title_id": title_id, "chapters": chapters_payload.get("volumes") or []}
            if parsed.get("kind") == "volume"
            else chapters_payload
        )
        previous_chapter, next_chapter = get_adjacent_chapters(navigation_payload, details["chapter_id"], resolved_lang)
        details.update(
            {
                "title_id": title_id,
                "images": images,
                "image_offsets": image_offsets,
                "image_count": len(images),
                "previous_chapter": previous_chapter,
                "next_chapter": next_chapter,
                "total_chapters": len((chapters_payload.get("volumes") if parsed.get("kind") == "volume" else chapters_payload.get("chapters")) or []),
            }
        )
        return details

    return await _dedup(f"reader:{cid}:{resolved_lang}", READER_TTL, _load)


def get_cached_chapter_reader_payload(chapter_ref: str, lang: str | None = None, title_hint: str = "") -> dict[str, Any] | None:
    parsed = _parse_chapter_id(chapter_ref)
    if not parsed:
        return None
    cid = _chapter_id(parsed["hid"], parsed["lang"], parsed["kind"], parsed["number"])
    cached = _cache_get(f"reader:{cid}:{_norm_lang(lang or parsed['lang'])}", READER_TTL)
    return dict(cached) if isinstance(cached, dict) else None


async def _filter_titles(*, limit: int, page: int = 1, type_name: str = "", sort: str = "recently_updated") -> list[dict[str, Any]]:
    params = [f"sort={quote(sort)}", "language%5B%5D=pt-br", f"page={max(1, int(page))}"]
    if type_name:
        params.insert(0, f"type={quote(type_name)}")
    html_text = await _request_text(f"{BASE_URL}/filter?{'&'.join(params)}")
    items = _parse_listing_titles(html_text)[: max(1, int(limit))]
    return await _enrich_items_with_mangabaka(items, limit=min(len(items), 12), network=True, cap_network=False)


async def get_title_search(search_type: str, limit: int = HOME_SECTION_LIMIT, **extra) -> list[dict[str, Any]]:
    search_type = _clean(search_type)
    page = int(extra.get("page") or 1)
    origin = _clean(extra.get("search_origin") or extra.get("origin") or extra.get("format") or "")
    sort_map = {
        "getFeatured": "trending",
        "getRecommend": "most_favourited",
        "getPopular": "most_viewed",
        "getRecentRead": "weekly_views",
        "getRecentChapterRead": "recently_updated",
        "getRecentlyUpdatedChapter": "recently_updated",
    }
    type_name = origin if origin in {"manga", "manhwa", "manhua"} else ""
    sort = sort_map.get(search_type, "recently_updated")
    return await _dedup(f"title-search:{search_type}:{type_name}:{sort}:{page}:{limit}", HOME_TTL, lambda: _filter_titles(limit=limit, page=page, type_name=type_name, sort=sort))


def get_cached_title_search(search_type: str, limit: int = HOME_SECTION_LIMIT, **extra) -> list[dict[str, Any]]:
    page = int(extra.get("page") or 1)
    origin = _clean(extra.get("search_origin") or extra.get("origin") or extra.get("format") or "")
    type_name = origin if origin in {"manga", "manhwa", "manhua"} else ""
    sort_map = {
        "getFeatured": "trending",
        "getRecommend": "most_favourited",
        "getPopular": "most_viewed",
        "getRecentRead": "weekly_views",
        "getRecentChapterRead": "recently_updated",
        "getRecentlyUpdatedChapter": "recently_updated",
    }
    cached = _cache_get(f"title-search:{search_type}:{type_name}:{sort_map.get(search_type, 'recently_updated')}:{page}:{limit}", HOME_TTL)
    return list(cached) if isinstance(cached, list) else []


async def get_origin_titles(origin: str, limit: int = HOME_SECTION_LIMIT, page: int = 1) -> list[dict[str, Any]]:
    origin = _clean(origin).lower()
    return await _filter_titles(limit=limit, page=page, type_name=origin if origin in {"manga", "manhwa", "manhua"} else "", sort="recently_updated")


async def get_recent_chapter_updates(limit: int = HOME_SECTION_LIMIT) -> list[dict[str, Any]]:
    items = await _filter_titles(limit=max(limit * 4, 32), page=1, sort="recently_updated")
    filtered = [
        item
        for item in items
        if item.get("chapter_id")
        and "-chapter-" in _clean(item.get("chapter_id")).lower()
        and _norm_lang(item.get("chapter_language") or item.get("language")) == "pt-br"
    ][:limit]
    return await _enrich_items_with_mangabaka(filtered, limit=min(limit, MANGABAKA_BATCH_NETWORK_LIMIT), network=True)


async def get_recent_chapters(limit: int = AUTO_POST_LIMIT) -> list[dict[str, Any]]:
    items = await get_recent_chapter_updates(limit)
    return await _enrich_items_with_mangabaka(items, limit=limit, network=True, cap_network=False)


async def get_home_payload(limit: int = HOME_SECTION_LIMIT) -> dict[str, Any]:
    limit = max(4, int(limit))
    featured, manga, manhwa, manhua, top, latest = await asyncio.gather(
        get_title_search("getFeatured", limit=min(limit, 10)),
        get_origin_titles("manga", limit=limit),
        get_origin_titles("manhwa", limit=limit),
        get_origin_titles("manhua", limit=limit),
        get_title_search("getPopular", limit=limit),
        get_recent_chapter_updates(limit=max(limit, 12)),
    )
    return {
        "featured": featured or top[: min(limit, 10)],
        "manga": manga,
        "manhwa": manhwa,
        "manhua": manhua,
        "recommended": top,
        "top_viewed": top,
        "latest_updates": latest,
        "recent_chapter_read": latest[:limit],
        "popular_season": featured,
        "popular": top,
        "recent_titles": latest[:limit],
        "latest_titles": latest,
        "recent_chapters": latest,
    }


def get_cached_home_snapshot(limit: int = HOME_SECTION_LIMIT) -> dict[str, Any]:
    return {
        "featured": get_cached_title_search("getFeatured", limit=min(limit, 10)),
        "manga": get_cached_title_search("getRecentChapterRead", limit=limit, search_origin="manga"),
        "manhwa": get_cached_title_search("getRecentChapterRead", limit=limit, search_origin="manhwa"),
        "manhua": get_cached_title_search("getRecentChapterRead", limit=limit, search_origin="manhua"),
        "recommended": get_cached_title_search("getPopular", limit=limit),
        "top_viewed": get_cached_title_search("getPopular", limit=limit),
        "latest_updates": get_cached_title_search("getRecentlyUpdatedChapter", limit=max(limit, 12)),
        "recent_chapter_read": get_cached_title_search("getRecentChapterRead", limit=limit),
        "popular_season": get_cached_title_search("getFeatured", limit=limit),
        "popular": get_cached_title_search("getPopular", limit=limit),
        "recent_titles": get_cached_title_search("getRecentChapterRead", limit=limit),
        "latest_titles": get_cached_title_search("getRecentlyUpdatedChapter", limit=max(limit, 12)),
        "recent_chapters": get_cached_title_search("getRecentlyUpdatedChapter", limit=max(limit, 12)),
    }


async def warm_catalog_cache(*, include_home: bool = True) -> None:
    if os.getenv("MANGAFIRE_WARMUP", "").strip().lower() not in {"1", "true", "yes"}:
        return
    if include_home:
        try:
            await get_home_payload(limit=8)
        except Exception as error:
            print("[MANGAFIRE][WARMUP]", repr(error))


def schedule_warm_catalog_cache() -> asyncio.Task | None:
    try:
        return asyncio.create_task(warm_catalog_cache())
    except RuntimeError:
        return None


def prefetch_title_bundles(title_refs: list[str], *, lang: str | None = None, limit: int = 3) -> asyncio.Task | None:
    refs = [ref for ref in title_refs if _clean(ref)][: max(1, int(limit or 1))]
    if not refs:
        return None
    async def runner():
        await asyncio.gather(*(get_title_bundle(ref, lang) for ref in refs), return_exceptions=True)
    try:
        return asyncio.create_task(runner())
    except RuntimeError:
        return None


def prefetch_reader_payloads(chapter_refs: list[str], *, lang: str | None = None, limit: int = 3) -> asyncio.Task | None:
    refs = [ref for ref in chapter_refs if _clean(ref)][: max(1, int(limit or 1))]
    if not refs:
        return None
    async def runner():
        await asyncio.gather(*(get_chapter_reader_payload(ref, lang) for ref in refs), return_exceptions=True)
    try:
        return asyncio.create_task(runner())
    except RuntimeError:
        return None


async def get_csrf_token(*args, **kwargs) -> str:
    return ""
