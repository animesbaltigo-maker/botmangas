from __future__ import annotations

import asyncio
import json
import os
import re
import time
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

def _detect_playwright_browser_root() -> str:
    candidates = [
        os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip(),
        "/app/.playwright",
        "/ms-playwright",
        str(Path.home() / ".cache" / "ms-playwright"),
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen or candidate == "0":
            continue
        seen.add(candidate)
        if Path(candidate).exists():
            return candidate
    return ""


_PLAYWRIGHT_BROWSER_ROOT = _detect_playwright_browser_root()
if not os.getenv("PLAYWRIGHT_BROWSERS_PATH") and _PLAYWRIGHT_BROWSER_ROOT:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _PLAYWRIGHT_BROWSER_ROOT
os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_GC", "1")

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover - optional runtime dependency
    async_playwright = None

from config import (
    API_CACHE_TTL_SECONDS,
    AUTO_POST_LIMIT,
    CATALOG_SITE_BASE,
    DATA_DIR,
    HOME_SECTION_LIMIT,
    PREFERRED_CHAPTER_LANG,
    RECENT_CHAPTER_TIME,
    SEARCH_LIMIT,
)
from core.http_client import get_http_client
from services.anilist_client import enrich_title_metadata
from services.metrics import get_search_seed_titles

BASE_URL = CATALOG_SITE_BASE.rstrip("/")

_HTTP_SEMAPHORE = asyncio.Semaphore(24)

_CACHE: dict[str, dict[str, Any]] = {}
_INFLIGHT: dict[str, asyncio.Task] = {}
_TITLE_URL_CACHE: dict[str, str] = {}
_TITLE_SUMMARY_CACHE: dict[str, dict[str, Any]] = {}
_TITLE_SUMMARY_CACHE_LOADED = False
_TITLE_SUMMARY_CACHE_SAVED_AT = 0.0
_CHAPTER_URL_CACHE: dict[str, str] = {}
_CHAPTER_TITLE_CACHE: dict[str, str] = {}
_CSRF_TOKEN: dict[str, Any] = {"value": "", "expires_at": 0.0}
_BROWSER_SESSION: dict[str, Any] = {"cookies": {}, "csrf_token": "", "expires_at": 0.0}
_BROWSER_SESSION_LOCK = asyncio.Lock()
_WARMUP_TASK: asyncio.Task | None = None

PLAYWRIGHT_SESSION_TTL = 1800
PLAYWRIGHT_NAV_TIMEOUT = 30000
PLAYWRIGHT_META_TIMEOUT = 10000
SEARCH_REMOTE_TIMEOUT = 8.0
SEARCH_QUICK_TIMEOUT = 3.4
SEARCH_RICH_TIMEOUT = 4.6
SEARCH_MIN_REMOTE_RESULTS = 6
SEARCH_CACHE_VERSION = "v2"
LOCAL_SEARCH_SEED_TTL = 300
FORM_QUICK_TIMEOUT = httpx.Timeout(5.5, connect=4.0, read=5.5, write=5.5, pool=5.5)
CHAPTER_LIST_QUICK_TIMEOUT = 4.2
CHAPTER_LIST_CACHED_TIMEOUT = httpx.Timeout(2.8, connect=2.0, read=2.8, write=2.8, pool=2.8)

SEARCH_TTL = min(max(API_CACHE_TTL_SECONDS, 180), 1800)
HOME_TTL = min(max(API_CACHE_TTL_SECONDS, 300), 1800)
TITLE_TTL = max(API_CACHE_TTL_SECONDS, 1800)
CHAPTERS_TTL = max(API_CACHE_TTL_SECONDS, 900)
CHAPTER_TTL = max(API_CACHE_TTL_SECONDS, 1800)
BUNDLE_TTL = max(API_CACHE_TTL_SECONDS, 1800)
READER_TTL = max(API_CACHE_TTL_SECONDS, 900)
TITLE_SUMMARY_CACHE_PATH = Path(DATA_DIR) / "title_summary_cache.json"
TITLE_SUMMARY_CACHE_LIMIT = 2500

KNOWN_STATUSES = {
    "ongoing",
    "completed",
    "hiatus",
    "cancelled",
    "dropped",
    "on going",
    "on-going",
}

STOP_DESCRIPTION_LABELS = {
    "keywords",
    "filters",
    "choose chapter",
    "comments",
    "related titles",
    "you may also like",
    "recommended",
}

TITLE_VARIANT_LABELS: list[tuple[str, str]] = [
    ("official colored", "Colorido"),
    ("digital colored comics", "Colorido"),
    ("full color", "Colorido"),
    ("full coloured", "Colorido"),
    ("colored", "Colorido"),
    ("coloured", "Colorido"),
    ("colorido", "Colorido"),
    ("colorida", "Colorido"),
    ("remake", "Remake"),
    ("redux", "Redux"),
    ("special", "Especial"),
    ("spin off", "Spin-off"),
    ("spin-off", "Spin-off"),
    ("one shot", "One-shot"),
    ("one-shot", "One-shot"),
    ("oneshot", "One-shot"),
    ("webtoon", "Webtoon"),
    ("manhwa", "Manhwa"),
    ("manhua", "Manhua"),
    ("novel", "Novel"),
]


def _resolve_playwright_executable() -> str:
    roots = [
        os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip(),
        _PLAYWRIGHT_BROWSER_ROOT,
        "/app/.playwright",
        "/ms-playwright",
        str(Path.home() / ".cache" / "ms-playwright"),
    ]
    patterns = [
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-win/chrome.exe",
        "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
        "chromium_headless_shell-*/chrome-win/headless_shell.exe",
        "chromium_headless_shell-*/chrome-mac/headless_shell",
    ]

    seen_roots: set[str] = set()
    for root_text in roots:
        if not root_text or root_text in seen_roots or root_text == "0":
            continue
        seen_roots.add(root_text)
        root = Path(root_text)
        if not root.exists():
            continue
        for pattern in patterns:
            matches = sorted(root.glob(pattern), reverse=True)
            for match in matches:
                if match.is_file():
                    return str(match)
    return ""


def _playwright_launch_kwargs() -> dict[str, Any]:
    executable_path = _resolve_playwright_executable()
    if executable_path:
        return {"headless": True, "executable_path": executable_path}
    return {"headless": True, "channel": "chromium"}


def clear_catalog_cache() -> None:
    global _TITLE_SUMMARY_CACHE_LOADED, _TITLE_SUMMARY_CACHE_SAVED_AT
    _CACHE.clear()
    _INFLIGHT.clear()
    _TITLE_URL_CACHE.clear()
    _TITLE_SUMMARY_CACHE.clear()
    _TITLE_SUMMARY_CACHE_LOADED = False
    _TITLE_SUMMARY_CACHE_SAVED_AT = 0.0
    _CHAPTER_URL_CACHE.clear()
    _CHAPTER_TITLE_CACHE.clear()
    _CSRF_TOKEN["value"] = ""
    _CSRF_TOKEN["expires_at"] = 0.0
    _BROWSER_SESSION["cookies"] = {}
    _BROWSER_SESSION["csrf_token"] = ""
    _BROWSER_SESSION["expires_at"] = 0.0


def _cache_get(key: str, ttl: int):
    item = _CACHE.get(key)
    if not item:
        return None
    if time.time() - item["time"] > ttl:
        _CACHE.pop(key, None)
        return None
    return item["data"]


def _cache_set(key: str, data: Any) -> Any:
    _CACHE[key] = {"time": time.time(), "data": data}
    return data


def get_cached_data(key: str, ttl: int) -> Any | None:
    return _cache_get(key, ttl)


async def _dedup_fetch(key: str, ttl: int, coro_factory):
    cached = _cache_get(key, ttl)
    if cached is not None:
        return cached

    task = _INFLIGHT.get(key)
    if task:
        return await task

    async def _runner():
        return await coro_factory()

    task = asyncio.create_task(_runner())
    _INFLIGHT[key] = task

    try:
        data = await task
        return _cache_set(key, data)
    finally:
        _INFLIGHT.pop(key, None)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _strip_html(value: Any) -> str:
    return BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)


def _absolute_url(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    return urljoin(f"{BASE_URL}/", text)


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", _clean(value).lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9\s-]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _clean_catalog_title(value: Any) -> str:
    title = _clean(value)
    if not title:
        return ""

    match = re.match(r"^(?P<main>.+?)\s+\((?P<rest>.+)\)$", title)
    if match:
        rest = match.group("rest")
        if len(rest) > 18 and ("/" in rest or "," in rest):
            title = match.group("main").strip()

    return title.strip(" -|")


def _slug_title_variant_hint(url: str) -> str:
    text = _clean(url)
    if not text:
        return ""

    match = re.search(r"/title-detail/([^/]+)-[a-f0-9]{20,32}", text, flags=re.IGNORECASE)
    if not match:
        return ""

    slug_text = _normalize_text(match.group(1).replace("-", " "))
    for needle, label in TITLE_VARIANT_LABELS:
        if needle in slug_text:
            return label
    return ""


def _title_variant_hint(raw_title: Any, url: str = "") -> str:
    title = _clean(raw_title)
    hints: list[str] = []

    match = re.match(r"^(?P<main>.+?)\s+\((?P<rest>.+)\)$", title)
    if match:
        rest = match.group("rest")
        for part in re.split(r"[/,;|]+", rest):
            normalized_part = _normalize_text(part)
            for needle, label in TITLE_VARIANT_LABELS:
                if needle in normalized_part and label not in hints:
                    hints.append(label)
                    break

    slug_hint = _slug_title_variant_hint(url)
    if slug_hint and slug_hint not in hints:
        hints.append(slug_hint)

    if not hints:
        return ""
    return " / ".join(hints[:2])


def _display_catalog_title(raw_title: Any, url: str = "") -> str:
    base_title = _clean_catalog_title(raw_title)
    if not base_title:
        return ""

    hint = _title_variant_hint(raw_title, url)
    if not hint:
        return base_title

    if _normalize_text(hint) in _normalize_text(base_title):
        return base_title
    return f"{base_title} [{hint}]"


def _search_score(query: str, title: str) -> tuple[int, int]:
    normalized_query = _normalize_text(query)
    normalized_title = _normalize_text(title)
    if not normalized_query or not normalized_title:
        return (0, 0)
    if normalized_title == normalized_query:
        return (500, -len(normalized_title))
    if normalized_title.startswith(normalized_query):
        return (400, -len(normalized_title))
    if f" {normalized_query}" in normalized_title or normalized_title.endswith(normalized_query):
        return (300, -len(normalized_title))
    if normalized_query in normalized_title:
        return (200, -len(normalized_title))
    query_words = normalized_query.split()
    title_words = normalized_title.split()
    overlap = len(set(query_words) & set(title_words))
    return (100 + overlap, -len(normalized_title))


def _iter_cached_search_candidates() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    for key, payload in list(_CACHE.items()):
        if not (key.startswith("title-search:") or key.startswith("smart-search:")):
            continue
        data = payload.get("data")
        if not isinstance(data, list):
            continue
        for item in data:
            if isinstance(item, dict):
                items.append(item)

    return items


def _iter_local_search_seed_candidates(limit: int = 300) -> list[dict[str, Any]]:
    cache_key = f"local-search-seeds:{max(1, int(limit))}"
    cached = _cache_get(cache_key, LOCAL_SEARCH_SEED_TTL)
    if isinstance(cached, list):
        return list(cached)

    try:
        items = list(get_search_seed_titles(limit=max(50, int(limit))))
    except Exception:
        items = []

    _cache_set(cache_key, items)
    return items


def _fallback_search_titles(query: str, limit: int) -> list[dict[str, Any]]:
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return []

    seen: set[str] = set()
    scored: list[tuple[tuple[int, int], dict[str, Any]]] = []

    fallback_candidates = _iter_cached_search_candidates()
    fallback_candidates.extend(_iter_local_search_seed_candidates(limit=max(120, int(limit) * 12)))

    for item in fallback_candidates:
        raw_title = item.get("display_title") or item.get("title") or item.get("name") or item.get("title_name")
        title = _clean_catalog_title(raw_title)
        title_id = _extract_title_id(item.get("title_id") or item.get("_id") or item.get("id") or item.get("url"))
        if not title or not title_id:
            continue

        score = _search_score(query, title)
        if score[0] < 200:
            continue

        if title_id in seen:
            continue
        seen.add(title_id)

        normalized_item = dict(item)
        normalized_item["title"] = title
        normalized_item["title_id"] = title_id
        normalized_item["display_title"] = _display_catalog_title(
            item.get("raw_title") or raw_title,
            item.get("url") or "",
        ) or title
        scored.append((score, normalized_item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[: max(1, int(limit))]]


def get_search_fallback_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    return list(_fallback_search_titles(query, limit))


def _search_cache_key(query: str, limit: int) -> str:
    return f"smart-search:{SEARCH_CACHE_VERSION}:{query.lower()}:{limit}"


def _search_cache_entry(query: str, limit: int) -> tuple[list[dict[str, Any]] | None, bool]:
    cache_key = _search_cache_key(query, limit)
    cached = _cache_get(cache_key, SEARCH_TTL)
    if cached is None:
        return None, False

    if isinstance(cached, dict) and isinstance(cached.get("items"), list):
        return list(cached.get("items") or []), bool(cached.get("partial"))

    if isinstance(cached, list):
        # Legacy cache shape from older versions; ignore suspicious short sets.
        items = list(cached)
        min_results = min(max(SEARCH_MIN_REMOTE_RESULTS, 1), max(1, int(limit)))
        partial = len(items) < min_results
        return items, partial

    return None, False


def _normalize_search_response_items(results: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    normalized = [
        item
        for item in (_normalize_catalog_item(raw_item) for raw_item in results if isinstance(raw_item, dict))
        if item.get("title_id")
    ]
    normalized.sort(key=lambda item: _search_score(query, item.get("title") or ""), reverse=True)
    return normalized


def _merge_search_result_sets(*result_sets: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    for result_set in result_sets:
        for item in result_set or []:
            title_id = _extract_title_id(item.get("title_id") or item.get("url") or "")
            if not title_id or title_id in seen:
                continue
            seen.add(title_id)
            merged.append(item)
            if len(merged) >= max(1, int(limit)):
                return merged

    return merged


def _is_auth_block_error(error: Exception | None) -> bool:
    if error is None:
        return False
    if isinstance(error, httpx.HTTPStatusError) and error.response is not None:
        return error.response.status_code in (401, 403)
    return "401" in repr(error) or "403" in repr(error)


def _extract_title_id(value: Any) -> str:
    text = _clean(value)
    match = re.search(r"title-detail/(?:[^/]*-)?([a-f0-9]{20,32})", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)

    match = re.search(r"\b([a-f0-9]{20,32})\b", text, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _extract_chapter_id(value: Any) -> str:
    text = _clean(value)
    match = re.search(r"chapter-detail/([a-f0-9]{20,32})", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)

    match = re.search(r"\b([a-f0-9]{20,32})\b", text, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _clean_og_title(raw: str) -> str:
    text = _clean(raw)
    text = re.sub(r"\s+-\s+Manga Ball$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+Online Gr[aá]tis.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+Online Free.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+V[aá]rios Idiomas.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+Multiple Languages.*$", "", text, flags=re.IGNORECASE)
    return text.strip(" -|")


def _clean_chapter_title(raw: str) -> tuple[str, str]:
    text = _clean_og_title(raw)
    chapter_number = ""

    match = re.search(r"\bCh\.\s*([0-9]+(?:\.[0-9]+)?)", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"\bCap[ií]tulo\s*([0-9]+(?:\.[0-9]+)?)", text, flags=re.IGNORECASE)
    if match:
        chapter_number = match.group(1)

    title = re.sub(r"\bCh\.\s*[0-9]+(?:\.[0-9]+)?\b.*$", "", text, flags=re.IGNORECASE)
    title = re.sub(r"\bCap[ií]tulo\s*[0-9]+(?:\.[0-9]+)?\b.*$", "", title, flags=re.IGNORECASE)
    return title.strip(" -|"), chapter_number


def _decimal_sort_value(value: Any) -> Decimal:
    text = _clean(value)
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        pass

    cleaned = re.sub(r"[^0-9.]", "", text)
    try:
        return Decimal(cleaned or "0")
    except InvalidOperation:
        return Decimal("0")


def _remember_title_url(title_id: str, url: str) -> None:
    title_id = _clean(title_id)
    url = _absolute_url(url)
    if title_id and url and "/title-detail/" in url:
        _TITLE_URL_CACHE[title_id] = url


def _restore_summary_indexes(title_id: str, item: dict[str, Any]) -> None:
    title_id = _extract_title_id(title_id) or _clean(title_id)
    if not title_id:
        return

    url = item.get("url") or item.get("title_url") or ""
    if url:
        _remember_title_url(title_id, url)

    chapter_id = item.get("chapter_id") or ""
    chapter_url = item.get("chapter_url") or ""
    if chapter_id and chapter_url:
        _remember_chapter_url(chapter_id, chapter_url)
    if chapter_id:
        _remember_chapter_title(chapter_id, title_id)


def _load_title_summary_cache_once() -> None:
    global _TITLE_SUMMARY_CACHE_LOADED
    if _TITLE_SUMMARY_CACHE_LOADED:
        return
    _TITLE_SUMMARY_CACHE_LOADED = True

    try:
        raw = json.loads(TITLE_SUMMARY_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return

    if not isinstance(raw, dict):
        return

    for title_id, item in raw.items():
        if not isinstance(item, dict):
            continue
        clean_id = _extract_title_id(title_id) or _extract_title_id(item.get("title_id")) or _clean(title_id)
        if clean_id:
            cached_item = dict(item)
            cached_item["title_id"] = clean_id
            _TITLE_SUMMARY_CACHE[clean_id] = cached_item
            _restore_summary_indexes(clean_id, cached_item)


def _persist_title_summary_cache(*, force: bool = False) -> None:
    global _TITLE_SUMMARY_CACHE_SAVED_AT
    now = time.time()
    if not force and now - _TITLE_SUMMARY_CACHE_SAVED_AT < 2.0:
        return

    _TITLE_SUMMARY_CACHE_SAVED_AT = now
    try:
        TITLE_SUMMARY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        items = sorted(
            _TITLE_SUMMARY_CACHE.items(),
            key=lambda pair: str(pair[1].get("cached_at") or pair[1].get("updated_at") or ""),
            reverse=True,
        )[:TITLE_SUMMARY_CACHE_LIMIT]
        data = {title_id: item for title_id, item in items}
        tmp_path = TITLE_SUMMARY_CACHE_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(TITLE_SUMMARY_CACHE_PATH)
    except Exception:
        pass


def _remember_title_summary(item: dict[str, Any]) -> None:
    _load_title_summary_cache_once()
    title_id = _extract_title_id(item.get("title_id")) or _clean(item.get("title_id"))
    if not title_id:
        return
    _restore_summary_indexes(title_id, item)

    current = _TITLE_SUMMARY_CACHE.get(title_id) or {}
    merged = dict(current)
    for key in (
        "title_id",
        "title",
        "display_title",
        "cover_url",
        "background_url",
        "status",
        "rating",
        "genres",
        "anilist_genres",
        "total_chapters",
        "anilist_chapters",
        "anilist_status",
        "anilist_score",
        "latest_chapter",
        "languages",
        "total_translations",
        "chapter_id",
        "chapter_url",
        "language",
        "url",
        "updated_at",
        "adult",
    ):
        value = item.get(key)
        if value not in (None, "", []):
            merged[key] = value

    merged["title_id"] = title_id
    merged["cached_at"] = str(int(time.time()))
    if merged != current:
        _TITLE_SUMMARY_CACHE[title_id] = merged
        _persist_title_summary_cache()


def get_cached_title_summary(title_id: str) -> dict[str, Any] | None:
    _load_title_summary_cache_once()
    title_id = _extract_title_id(title_id) or _clean(title_id)
    if not title_id:
        return None
    cached = _TITLE_SUMMARY_CACHE.get(title_id)
    if cached:
        _restore_summary_indexes(title_id, cached)
        return dict(cached)

    for item in _iter_local_search_seed_candidates(limit=800):
        candidate_id = _extract_title_id(item.get("title_id") or item.get("url") or "")
        title = _clean_catalog_title(item.get("display_title") or item.get("title") or "")
        if candidate_id == title_id and title:
            summary = {
                "title_id": title_id,
                "title": title,
                "display_title": item.get("display_title") or title,
            }
            _remember_title_summary(summary)
            return dict(_TITLE_SUMMARY_CACHE.get(title_id) or summary)

    return None


def _remember_chapter_url(chapter_id: str, url: str) -> None:
    chapter_id = _clean(chapter_id)
    url = _absolute_url(url)
    if chapter_id and url:
        _CHAPTER_URL_CACHE[chapter_id] = url


def _remember_chapter_title(chapter_id: str, title_id: str) -> None:
    chapter_id = _extract_chapter_id(chapter_id) or _clean(chapter_id)
    title_id = _extract_title_id(title_id) or _clean(title_id)
    if chapter_id and title_id:
        _CHAPTER_TITLE_CACHE[chapter_id] = title_id


def _extract_meta_content(soup: BeautifulSoup, prop: str) -> str:
    node = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    if not node:
        return ""
    return _clean(node.get("content"))


def _browser_session_is_valid() -> bool:
    return (
        bool(_BROWSER_SESSION.get("cookies"))
        and bool(_clean(_BROWSER_SESSION.get("csrf_token")))
        and time.time() < float(_BROWSER_SESSION.get("expires_at") or 0.0)
    )


def _browser_session_snapshot() -> dict[str, Any] | None:
    if not _browser_session_is_valid():
        return None
    return {
        "cookies": dict(_BROWSER_SESSION["cookies"]),
        "csrf_token": _BROWSER_SESSION["csrf_token"],
        "expires_at": _BROWSER_SESSION["expires_at"],
    }


async def _prepare_playwright_page(context, page) -> None:
    async def _route(route):
        if route.request.resource_type in {"image", "media", "font", "stylesheet"}:
            await route.abort()
            return
        await route.continue_()

    try:
        await context.route("**/*", _route)
    except Exception:
        pass

    await page.goto(
        f"{BASE_URL}/",
        wait_until="domcontentloaded",
        timeout=PLAYWRIGHT_NAV_TIMEOUT,
    )

    try:
        await page.locator("meta[name='csrf-token']").wait_for(timeout=PLAYWRIGHT_META_TIMEOUT)
    except Exception:
        pass


async def _ensure_browser_session(force_refresh: bool = False) -> dict[str, Any]:
    if not force_refresh and _browser_session_is_valid():
        return {
            "cookies": dict(_BROWSER_SESSION["cookies"]),
            "csrf_token": _BROWSER_SESSION["csrf_token"],
            "expires_at": _BROWSER_SESSION["expires_at"],
        }

    if async_playwright is None:
        raise RuntimeError(
            "Playwright nao esta instalado. Instale 'playwright' e rode "
            "'python -m playwright install chromium'."
        )

    async with _BROWSER_SESSION_LOCK:
        if not force_refresh and _browser_session_is_valid():
            return {
                "cookies": dict(_BROWSER_SESSION["cookies"]),
                "csrf_token": _BROWSER_SESSION["csrf_token"],
                "expires_at": _BROWSER_SESSION["expires_at"],
            }

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**_playwright_launch_kwargs())
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="pt-BR",
                )
                page = await context.new_page()
                await _prepare_playwright_page(context, page)

                csrf_token = _clean(await page.locator("meta[name='csrf-token']").get_attribute("content"))
                cookies = {
                    _clean(item.get("name")): _clean(item.get("value"))
                    for item in (await context.cookies())
                    if _clean(item.get("name")) and _clean(item.get("value"))
                }
            finally:
                await browser.close()

        if not csrf_token or not cookies:
            raise RuntimeError("Nao foi possivel obter a sessao protegida da fonte.")

        _BROWSER_SESSION["cookies"] = cookies
        _BROWSER_SESSION["csrf_token"] = csrf_token
        _BROWSER_SESSION["expires_at"] = time.time() + PLAYWRIGHT_SESSION_TTL

        return {
            "cookies": dict(cookies),
            "csrf_token": csrf_token,
            "expires_at": _BROWSER_SESSION["expires_at"],
        }


def _build_ajax_referer(path: str, data: dict[str, Any]) -> str:
    normalized_path = _clean(path).lower()
    title_id = _extract_title_id(data.get("title_id"))
    if "chapter-listing-by-title-id" in normalized_path and title_id:
        return _TITLE_URL_CACHE.get(title_id) or _absolute_url(f"/title-detail/{title_id}/")
    return f"{BASE_URL}/"


async def _build_ajax_headers(
    session: dict[str, Any] | None = None,
    referer: str | None = None,
    *,
    allow_browser_token: bool = True,
) -> dict[str, str]:
    csrf_token = _clean((session or {}).get("csrf_token"))
    if not csrf_token:
        csrf_token = await get_csrf_token(allow_browser=allow_browser_token)

    referer = _absolute_url(referer) or f"{BASE_URL}/"
    return {
        "Accept": "application/json,text/plain,*/*",
        "X-CSRF-TOKEN": csrf_token,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "Origin": BASE_URL,
    }


async def _request_form_json_via_playwright(path: str, data: dict[str, Any], referer: str) -> dict[str, Any]:
    if async_playwright is None:
        raise RuntimeError(
            "Playwright nao esta instalado. Instale 'playwright' e rode "
            "'python -m playwright install chromium'."
        )

    url = _absolute_url(path)
    referer = _absolute_url(referer) or f"{BASE_URL}/"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**_playwright_launch_kwargs())
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="pt-BR",
            )
            page = await context.new_page()
            await _prepare_playwright_page(context, page)
            headers = await _build_ajax_headers(
                {
                    "csrf_token": _clean(await page.locator("meta[name='csrf-token']").get_attribute("content")),
                },
                referer,
            )
            response = await context.request.post(
                url,
                form=data,
                headers=headers,
                timeout=PLAYWRIGHT_NAV_TIMEOUT,
            )
            response_text = await response.text()
        finally:
            await browser.close()

    if response.status != 200:
        raise RuntimeError(f"Playwright recebeu HTTP {response.status} ao consultar {url}.")

    try:
        return json.loads(response_text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Resposta invalida da fonte em {url}: {response_text[:240]!r}") from error


def _extract_text_lines(soup: BeautifulSoup) -> list[str]:
    lines: list[str] = []
    for raw in soup.get_text("\n").splitlines():
        text = _clean(raw)
        if text:
            lines.append(text)
    return lines


def _find_title_anchor_index(lines: list[str], title: str) -> int:
    normalized_title = _normalize_text(title)
    if not normalized_title:
        return 0

    for index, line in enumerate(lines):
        normalized_line = _normalize_text(line)
        if not normalized_line:
            continue
        if normalized_line == normalized_title:
            return index
        if normalized_title in normalized_line and "online free" not in normalized_line:
            return index
    return 0


def _parse_list_line(line: str, separator: str = ",") -> list[str]:
    values: list[str] = []
    seen: set[str] = set()

    for part in [item.strip() for item in line.split(separator)]:
        if not part:
            continue
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        values.append(part)
    return values


def _parse_title_detail_html(html_text: str, requested_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")

    og_title = _extract_meta_content(soup, "og:title")
    og_image = _extract_meta_content(soup, "og:image")
    og_url = _extract_meta_content(soup, "og:url")
    title_id_match = re.search(r"const titleId = ['`]([a-f0-9]{20,32})['`]", html_text, flags=re.IGNORECASE)
    title_id = title_id_match.group(1) if title_id_match else _extract_title_id(og_url or requested_url)

    title = _clean_og_title(og_title)
    lines = _extract_text_lines(soup)
    anchor_index = _find_title_anchor_index(lines, title)
    title_window = lines[anchor_index: anchor_index + 80] if lines else []

    genres: list[str] = []
    alt_titles: list[str] = []
    authors: list[str] = []
    description = ""
    published = ""
    status = ""
    rating = ""
    followers = ""
    views = ""
    comments = ""
    source_total_chapters = None
    source_total_translations = None

    primary_genres: list[str] = []
    for line in title_window[:12]:
        if "," not in line:
            continue
        normalized = line.lower()
        if "published:" in normalized or "/" in line:
            continue
        maybe_genres = [item for item in _parse_list_line(line) if len(item) <= 24]
        if len(maybe_genres) >= 2:
            primary_genres = maybe_genres
            break

    for line in title_window:
        normalized = line.lower()
        if normalized.startswith("published:"):
            published = _clean(line.split(":", 1)[1])
            chapter_count_match = re.search(r"\b(\d+)\s+chapters?\b", line, flags=re.IGNORECASE)
            if chapter_count_match:
                source_total_chapters = int(chapter_count_match.group(1))
            continue
        chapter_summary_match = re.search(
            r"\b(\d+)\s+chapters?\s+with\s+(\d+)\s+translations?\b",
            line,
            flags=re.IGNORECASE,
        )
        if chapter_summary_match:
            source_total_chapters = int(chapter_summary_match.group(1))
            source_total_translations = int(chapter_summary_match.group(2))
            continue
        if normalized in KNOWN_STATUSES:
            status = line
            continue
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", line):
            if not rating:
                rating = line
            elif not followers:
                followers = line
            elif not views:
                views = line
            elif not comments:
                comments = line

    keywords_index = next((index for index, line in enumerate(lines) if line.lower() == "keywords"), -1)
    if not primary_genres and keywords_index >= 0:
        for line in lines[keywords_index + 1: keywords_index + 10]:
            normalized = line.lower()
            if normalized in STOP_DESCRIPTION_LABELS:
                break
            if "keywords" in normalized or re.search(r"\d", line):
                continue
            if len(line) > 24 and "," not in line:
                break
            genres.extend(_parse_list_line(line))

    if primary_genres:
        genres = primary_genres

    published_index = next((index for index, line in enumerate(title_window) if line.lower().startswith("published:")), -1)
    if published_index > 0:
        for line in reversed(title_window[:published_index]):
            if line == title or line in alt_titles or line in genres:
                continue
            if len(line) > 60 or ":" in line or line.lower() in KNOWN_STATUSES:
                continue
            authors = [line]
            break

    description_index = next((index for index, line in enumerate(lines) if line.lower() == "description"), -1)
    if description_index >= 0:
        description_lines: list[str] = []
        for line in lines[description_index + 1:]:
            normalized = line.lower()
            if normalized in STOP_DESCRIPTION_LABELS:
                break
            if line == "Expand":
                continue
            description_lines.append(line)
        description = _clean(" ".join(description_lines))

    result = {
        "title_id": title_id,
        "url": _absolute_url(og_url or requested_url),
        "title": title,
        "alt_titles": [],
        "description": description,
        "cover_url": _absolute_url(og_image),
        "background_url": _absolute_url(og_image),
        "status": status,
        "rating": rating,
        "followers": followers,
        "views": views,
        "comments": comments,
        "source_total_chapters": source_total_chapters,
        "source_total_translations": source_total_translations,
        "genres": genres,
        "authors": authors,
        "published": published,
    }

    _remember_title_url(title_id, result["url"])
    return result


def _parse_chapter_detail_html(html_text: str, requested_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")

    og_title = _extract_meta_content(soup, "og:title")
    og_image = _extract_meta_content(soup, "og:image")
    og_url = _extract_meta_content(soup, "og:url")
    manga_title, og_chapter_number = _clean_chapter_title(og_title)

    title_id_match = re.search(r"const titleId = ['`]([a-f0-9]{20,32})['`]", html_text, flags=re.IGNORECASE)
    if not title_id_match:
        title_id_match = re.search(r'"titleId"\s*:\s*"([a-f0-9]{20,32})"', html_text, flags=re.IGNORECASE)
    if not title_id_match:
        title_id_match = re.search(r'"title_id"\s*:\s*"([a-f0-9]{20,32})"', html_text, flags=re.IGNORECASE)
    chapter_id_match = re.search(r"const chapterId = ['`]([a-f0-9]{20,32})['`]", html_text, flags=re.IGNORECASE)
    chapter_number_match = re.search(r"const chapterNumber = ['`]([^'`]+)['`]", html_text, flags=re.IGNORECASE)
    chapter_volume_match = re.search(r"const chapterVolume = ['`]([^'`]+)['`]", html_text, flags=re.IGNORECASE)
    chapter_language_match = re.search(r"const chapterLanguage = ['`]([^'`]+)['`]", html_text, flags=re.IGNORECASE)
    images_match = re.search(
        r"const chapterImages = JSON\.parse\(`(?P<data>\[.*?\])`\);",
        html_text,
        flags=re.DOTALL,
    )

    title_id = title_id_match.group(1) if title_id_match else ""
    chapter_id = chapter_id_match.group(1) if chapter_id_match else _extract_chapter_id(og_url or requested_url)
    chapter_number = _clean(chapter_number_match.group(1)) if chapter_number_match else og_chapter_number
    chapter_volume = _clean(chapter_volume_match.group(1)) if chapter_volume_match else ""
    chapter_language = _clean(chapter_language_match.group(1)).lower() if chapter_language_match else PREFERRED_CHAPTER_LANG

    chapter_images: list[str] = []
    if images_match:
        try:
            raw_images = json.loads(images_match.group("data"))
            chapter_images = [_absolute_url(item) for item in raw_images if _clean(item)]
        except Exception:
            chapter_images = []

    title_detail_match = re.search(r"https?://[^\"'`\s]+/title-detail/[^\"'`\s]+", html_text)
    title_detail_url = _absolute_url(title_detail_match.group(0)) if title_detail_match else ""
    if not title_detail_url:
        anchor = soup.select_one("a[href*='/title-detail/']")
        if anchor:
            title_detail_url = _absolute_url(anchor.get("href"))
    if not title_detail_url:
        canonical = soup.find("link", attrs={"rel": "canonical"})
        canonical_href = _clean(canonical.get("href") if canonical else "")
        if "/title-detail/" in canonical_href:
            title_detail_url = _absolute_url(canonical_href)

    result = {
        "title_id": title_id or _extract_title_id(title_detail_url),
        "title": manga_title,
        "title_url": title_detail_url,
        "chapter_id": chapter_id,
        "chapter_number": chapter_number,
        "chapter_volume": chapter_volume,
        "chapter_language": chapter_language,
        "chapter_url": _absolute_url(og_url or requested_url),
        "cover_url": _absolute_url(og_image),
        "images": chapter_images,
        "image_count": len(chapter_images),
    }

    _remember_chapter_url(chapter_id, result["chapter_url"])
    if result["title_id"] and title_detail_url:
        _remember_title_url(result["title_id"], title_detail_url)

    return result


def _normalize_catalog_item(item: dict[str, Any]) -> dict[str, Any]:
    url = _absolute_url(item.get("url") or item.get("href") or item.get("link"))
    chapter_url = _absolute_url(
        item.get("chapter_url")
        or item.get("latest_chapter_url")
        or item.get("read_url")
        or item.get("latest_url")
    )

    if url and "/chapter-detail/" in url and not chapter_url:
        chapter_url = url

    title_id = (
        _clean(item.get("title_id"))
        or _clean(item.get("_id"))
        or _clean(item.get("id"))
        or _extract_title_id(url)
        or _extract_title_id(chapter_url)
    )
    chapter_id = _clean(item.get("chapter_id")) or _extract_chapter_id(chapter_url or url)
    raw_title = item.get("name") or item.get("title") or item.get("title_name")
    clean_title = _clean_catalog_title(raw_title)
    display_title = _display_catalog_title(raw_title, url)

    normalized = {
        "title_id": title_id,
        "chapter_id": chapter_id,
        "url": url,
        "chapter_url": chapter_url,
        "title": clean_title,
        "display_title": display_title or clean_title,
        "raw_title": _clean(raw_title),
        "cover_url": _absolute_url(item.get("cover") or item.get("img") or item.get("image") or item.get("thumbnail")),
        "background_url": _absolute_url(item.get("background") or item.get("cover") or item.get("img")),
        "status": _strip_html(item.get("status") or item.get("status_label") or item.get("statusText")),
        "rating": _clean(item.get("rating") or item.get("score")),
        "followers": _clean(item.get("followers") or item.get("bookmark")),
        "views": _clean(item.get("views")),
        "updated_at": _clean(item.get("updated_at") or item.get("updatedAt") or item.get("latest")),
        "language": _clean(item.get("language") or item.get("lang") or item.get("language_code")).lower(),
        "language_flag": _absolute_url(item.get("languageFlag") or item.get("flag")),
        "latest_chapter": _clean(
            item.get("chapter")
            or item.get("latest_chapter")
            or item.get("updated_chapter")
            or item.get("chapter_number")
        ),
        "adult": bool(item.get("isAdult") or item.get("adult") or item.get("is_adult")),
    }

    _remember_title_url(title_id, url)
    _remember_title_summary(normalized)
    if chapter_id and chapter_url:
        _remember_chapter_url(chapter_id, chapter_url)
    return normalized


def _normalize_translation(raw: dict[str, Any], group: dict[str, Any]) -> dict[str, Any]:
    url = _absolute_url(raw.get("url"))
    translation = {
        "id": _clean(raw.get("id")) or _extract_chapter_id(url),
        "url": url,
        "language": _clean(raw.get("language") or raw.get("lang")).lower(),
        "volume": _clean(raw.get("volume") or group.get("volume")),
        "name": _clean(raw.get("name") or raw.get("chapter_name") or group.get("name")),
        "group_name": _clean((raw.get("group") or {}).get("name") if isinstance(raw.get("group"), dict) else raw.get("group")),
        "date": _clean(raw.get("date") or raw.get("updated_at")),
        "views": _clean(raw.get("views")),
        "likes": _clean(raw.get("likes")),
        "comments": _clean(raw.get("comments")),
    }
    _remember_chapter_url(translation["id"], url)
    return translation


def _pick_translation(translations: list[dict[str, Any]], preferred_lang: str) -> dict[str, Any] | None:
    if not translations:
        return None

    preferred_lang = _clean(preferred_lang).lower()
    if preferred_lang:
        for translation in translations:
            if translation["language"] == preferred_lang:
                return translation

    for fallback_lang in (PREFERRED_CHAPTER_LANG, "pt-br", "en"):
        for translation in translations:
            if translation["language"] == fallback_lang:
                return translation

    return translations[0]


def _normalize_chapter_groups(raw_groups: list[dict[str, Any]], preferred_lang: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []

    for raw_group in raw_groups or []:
        chapter_number = _clean(raw_group.get("number") or raw_group.get("chapter"))
        chapter_number_float = _decimal_sort_value(raw_group.get("number_float") or chapter_number or raw_group.get("number"))
        translations = [
            _normalize_translation(raw_translation, raw_group)
            for raw_translation in (raw_group.get("translations") or [])
        ]
        preferred = _pick_translation(translations, preferred_lang)

        normalized.append(
            {
                "chapter_number": chapter_number,
                "chapter_number_float": str(chapter_number_float),
                "sort_value": chapter_number_float,
                "translations": translations,
                "preferred_translation": preferred,
            }
        )

    normalized.sort(key=lambda item: item["sort_value"], reverse=True)
    return normalized


async def _request_text(url: str, *, headers: dict[str, str] | None = None) -> str:
    client = await get_http_client()
    last_error: Exception | None = None

    for attempt in range(3):
        try:
            async with _HTTP_SEMAPHORE:
                response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.text
        except (httpx.HTTPError, httpx.TimeoutException) as error:
            last_error = error
            await asyncio.sleep(0.35 * (attempt + 1))

    raise RuntimeError(f"Falha ao buscar {url}: {last_error!r}")


async def _request_text_fast(url: str, *, headers: dict[str, str] | None = None) -> str:
    client = await get_http_client()

    async with _HTTP_SEMAPHORE:
        response = await client.get(url, headers=headers, timeout=FORM_QUICK_TIMEOUT)

    response.raise_for_status()
    return response.text


async def _try_request_text(url: str) -> str:
    try:
        return await _request_text(url)
    except Exception:
        return ""


async def _request_form_json(path: str, data: dict[str, Any]) -> dict[str, Any]:
    client = await get_http_client()
    url = _absolute_url(path)
    referer = _build_ajax_referer(path, data)

    last_error: Exception | None = None
    browser_error: Exception | None = None

    browser_session = _browser_session_snapshot()
    headers = await _build_ajax_headers(
        browser_session,
        referer,
        allow_browser_token=bool(browser_session),
    )
    cookies = dict((browser_session or {}).get("cookies") or {})

    for attempt in range(3):
        try:
            async with _HTTP_SEMAPHORE:
                response = await client.post(
                    url,
                    data=data,
                    headers=headers,
                    cookies=cookies or None,
                )
            if response.status_code in (401, 403):
                last_error = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
                try:
                    browser_session = await _ensure_browser_session(force_refresh=True)
                    headers = await _build_ajax_headers(browser_session, referer)
                    cookies = dict(browser_session.get("cookies") or {})
                except Exception as error:
                    browser_error = error
                await asyncio.sleep(0.35 * (attempt + 1))
                continue
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, httpx.TimeoutException, ValueError) as error:
            last_error = error
            if isinstance(error, httpx.HTTPStatusError) and error.response is not None:
                if error.response.status_code in (401, 403):
                    try:
                        browser_session = await _ensure_browser_session(force_refresh=True)
                        headers = await _build_ajax_headers(browser_session, referer)
                        cookies = dict(browser_session.get("cookies") or {})
                    except Exception as browser_refresh_error:
                        browser_error = browser_refresh_error
            await asyncio.sleep(0.35 * (attempt + 1))

    if isinstance(last_error, httpx.HTTPStatusError) and last_error.response is not None:
        if last_error.response.status_code in (401, 403):
            try:
                return await _request_form_json_via_playwright(path, data, referer)
            except Exception as error:
                browser_error = error

    if browser_error and (
        not isinstance(last_error, httpx.HTTPStatusError)
        or (last_error.response is not None and last_error.response.status_code in (401, 403))
    ):
        raise RuntimeError(
            f"Falha ao consultar {url}: a fonte exigiu sessao de navegador "
            f"e o fallback nao ficou disponivel ({browser_error!r})."
        )

    raise RuntimeError(f"Falha ao consultar {url}: {last_error!r}")


async def _request_form_json_quick(path: str, data: dict[str, Any]) -> dict[str, Any]:
    client = await get_http_client()
    url = _absolute_url(path)
    referer = _build_ajax_referer(path, data)
    headers = await _build_ajax_headers(None, referer, allow_browser_token=False)
    last_error: Exception | None = None

    for attempt in range(2):
        try:
            async with _HTTP_SEMAPHORE:
                response = await client.post(
                    url,
                    data=data,
                    headers=headers,
                    timeout=FORM_QUICK_TIMEOUT,
                )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, httpx.TimeoutException, ValueError) as error:
            last_error = error
            if attempt == 0:
                await asyncio.sleep(0.18)

    raise RuntimeError(f"Falha ao consultar {url}: {last_error!r}")


async def _request_form_json_cached_quick(path: str, data: dict[str, Any]) -> dict[str, Any]:
    client = await get_http_client()
    url = _absolute_url(path)
    referer = _absolute_url(_build_ajax_referer(path, data)) or f"{BASE_URL}/"
    csrf_token = ""
    cookies: dict[str, str] = {}

    if _CSRF_TOKEN["value"] and time.time() < _CSRF_TOKEN["expires_at"]:
        csrf_token = _clean(_CSRF_TOKEN["value"])
    elif _browser_session_is_valid():
        csrf_token = _clean(_BROWSER_SESSION.get("csrf_token"))
        cookies = dict(_BROWSER_SESSION.get("cookies") or {})

    headers = {
        "Accept": "application/json,text/plain,*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "Origin": BASE_URL,
    }
    if csrf_token:
        headers["X-CSRF-TOKEN"] = csrf_token

    async with _HTTP_SEMAPHORE:
        response = await client.post(
            url,
            data=data,
            headers=headers,
            cookies=cookies or None,
            timeout=CHAPTER_LIST_CACHED_TIMEOUT,
        )
    response.raise_for_status()
    return response.json()


async def get_csrf_token(force_refresh: bool = False, *, allow_browser: bool = True) -> str:
    if not force_refresh and _CSRF_TOKEN["value"] and time.time() < _CSRF_TOKEN["expires_at"]:
        return _CSRF_TOKEN["value"]

    async def _token_from_html(*, fast: bool) -> str:
        html_text = await (_request_text_fast(BASE_URL) if fast else _request_text(BASE_URL))
        soup = BeautifulSoup(html_text, "html.parser")
        meta = soup.find("meta", attrs={"name": "csrf-token"})
        token_value = _clean(meta.get("content") if meta else "")
        if not token_value:
            raise RuntimeError("Nao foi possivel obter o token CSRF da fonte.")
        return token_value

    try:
        token = await _token_from_html(fast=True)
        _CSRF_TOKEN["value"] = token
        _CSRF_TOKEN["expires_at"] = time.time() + 1800
        return token
    except Exception:
        pass

    if allow_browser:
        try:
            browser_session = await _ensure_browser_session(force_refresh=force_refresh)
            token = _clean(browser_session.get("csrf_token"))
            if token:
                _CSRF_TOKEN["value"] = token
                _CSRF_TOKEN["expires_at"] = time.time() + 1800
                return token
        except Exception:
            pass

    token = await _token_from_html(fast=not allow_browser)
    _CSRF_TOKEN["value"] = token
    _CSRF_TOKEN["expires_at"] = time.time() + 1800
    return token


async def get_title_search(search_type: str, limit: int = HOME_SECTION_LIMIT, **extra) -> list[dict[str, Any]]:
    cache_key = f"title-search:{search_type}:{limit}:{json.dumps(extra, sort_keys=True)}"

    async def _load():
        payload: dict[str, Any] = {
            "search_type": search_type,
            "search_limit": max(1, int(limit)),
        }
        payload.update({key: value for key, value in extra.items() if value not in (None, "")})

        response = await _request_form_json("/api/v1/title/search/", payload)
        if response.get("code") != 200:
            return []

        data = response.get("data") or []
        return [
            normalized
            for normalized in (_normalize_catalog_item(item) for item in data if isinstance(item, dict))
            if normalized.get("title_id") or normalized.get("chapter_id")
        ]

    return await _dedup_fetch(cache_key, HOME_TTL, _load)


def get_cached_title_search(search_type: str, limit: int = HOME_SECTION_LIMIT, **extra) -> list[dict[str, Any]]:
    cache_key = f"title-search:{search_type}:{limit}:{json.dumps(extra, sort_keys=True)}"
    cached = _cache_get(cache_key, HOME_TTL)
    return list(cached or [])


async def search_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    normalized_query = _clean(query)
    if not normalized_query:
        return []

    cache_key = _search_cache_key(normalized_query, limit)
    cached_items, cached_partial = _search_cache_entry(normalized_query, limit)
    if cached_items is not None and not cached_partial:
        return cached_items

    task = _INFLIGHT.get(cache_key)
    if task:
        return await task

    async def _load() -> tuple[list[dict[str, Any]], bool]:
        fallback_results = _fallback_search_titles(normalized_query, limit)
        payload = {
            "search_input": normalized_query,
            "search_limit": max(8, int(limit)),
            "limit": max(8, int(limit)),
        }
        quick_results: list[dict[str, Any]] = []
        rich_results: list[dict[str, Any]] = []
        quick_error: Exception | None = None
        rich_error: Exception | None = None

        try:
            quick_response = await asyncio.wait_for(
                _request_form_json_quick("/api/v1/smart-search/search/", payload),
                timeout=min(SEARCH_REMOTE_TIMEOUT, SEARCH_QUICK_TIMEOUT),
            )
            if quick_response.get("code") == 200:
                quick_results = _normalize_search_response_items(
                    (quick_response.get("data") or {}).get("manga") or [],
                    normalized_query,
                )
        except Exception as error:
            quick_error = error

        should_try_rich = (
            len(quick_results) < min(max(SEARCH_MIN_REMOTE_RESULTS, 1), max(1, int(limit)))
            or bool(fallback_results)
            or _is_auth_block_error(quick_error)
        )

        if should_try_rich:
            try:
                rich_timeout = SEARCH_REMOTE_TIMEOUT if _is_auth_block_error(quick_error) else min(
                    SEARCH_REMOTE_TIMEOUT,
                    SEARCH_RICH_TIMEOUT,
                )
                rich_response = await asyncio.wait_for(
                    _request_form_json("/api/v1/smart-search/search/", payload),
                    timeout=rich_timeout,
                )
                if rich_response.get("code") == 200:
                    rich_results = _normalize_search_response_items(
                        (rich_response.get("data") or {}).get("manga") or [],
                        normalized_query,
                    )
            except Exception as error:
                rich_error = error
                schedule_warm_catalog_cache()

        merged = _merge_search_result_sets(quick_results, rich_results, fallback_results, limit=limit)
        if merged:
            return merged, not bool(quick_results or rich_results)
        if fallback_results:
            return fallback_results, True
        if quick_error is not None and not _is_auth_block_error(quick_error):
            raise quick_error
        if rich_error is not None and not _is_auth_block_error(rich_error):
            raise rich_error
        return [], False

    async def _runner():
        items, partial = await _load()
        if not partial:
            _cache_set(cache_key, {"items": items, "partial": False})
        return items

    task = asyncio.create_task(_runner())
    _INFLIGHT[cache_key] = task
    try:
        return await task
    finally:
        _INFLIGHT.pop(cache_key, None)


async def search_titles_fast(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    normalized_query = _clean(query)
    if not normalized_query:
        return []

    cached_items, cached_partial = _search_cache_entry(normalized_query, limit)
    if cached_items is not None and not cached_partial:
        return cached_items

    fallback_results = _fallback_search_titles(normalized_query, limit)
    payload = {
        "search_input": normalized_query,
        "search_limit": max(8, int(limit)),
        "limit": max(8, int(limit)),
    }

    try:
        quick_response = await asyncio.wait_for(
            _request_form_json_quick("/api/v1/smart-search/search/", payload),
            timeout=min(SEARCH_REMOTE_TIMEOUT, SEARCH_QUICK_TIMEOUT),
        )
    except Exception:
        return fallback_results

    quick_results: list[dict[str, Any]] = []
    if quick_response.get("code") == 200:
        quick_results = _normalize_search_response_items(
            (quick_response.get("data") or {}).get("manga") or [],
            normalized_query,
        )

    merged = _merge_search_result_sets(quick_results, fallback_results, limit=limit)
    if quick_results:
        min_results = min(max(SEARCH_MIN_REMOTE_RESULTS, 1), max(1, int(limit)))
        _cache_set(
            _search_cache_key(normalized_query, limit),
            {"items": merged, "partial": len(merged) < min_results},
        )
    return merged


def get_cached_search_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]] | None:
    normalized_query = _clean(query)
    if not normalized_query:
        return []
    cached, partial = _search_cache_entry(normalized_query, limit)
    if cached is None or partial:
        return None
    return cached


def _chapter_count_from_summary(summary: dict[str, Any]) -> int:
    for key in ("total_chapters", "chapters_count", "chapter_count", "anilist_chapters"):
        value = summary.get(key)
        if value in (None, "", []):
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _title_bundle_cache_key(title_ref: str, resolved_lang: str) -> str:
    title_ref = _clean(title_ref)
    title_id = _extract_title_id(title_ref)
    return f"title-bundle:{title_ref if '/title-detail/' in title_ref else title_id or title_ref}:{resolved_lang}"


def _chapter_list_payload(title_id: str) -> dict[str, Any]:
    # MangaBall returns every translation in one payload; language filtering happens client-side.
    return {"title_id": title_id, "userSettingsEnabled": "false"}


def _chapter_list_cache_key(title_id: str) -> str:
    return f"chapter-list:{title_id}:all"


def get_cached_chapter_list(title_id: str, lang: str | None = None) -> dict[str, Any] | None:
    title_id = _extract_title_id(title_id) or _clean(title_id)
    if not title_id:
        return None
    cached = _cache_get(_chapter_list_cache_key(title_id), CHAPTERS_TTL)
    if isinstance(cached, dict) and not cached.get("partial"):
        return _chapter_payload_with_preferred_language(cached, lang)
    return None


def _chapter_payload_with_preferred_language(chapter_payload: dict[str, Any], preferred_lang: str | None) -> dict[str, Any]:
    preferred_lang = _clean(preferred_lang).lower() or PREFERRED_CHAPTER_LANG
    chapters: list[dict[str, Any]] = []
    for chapter in chapter_payload.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        copy = dict(chapter)
        copy["preferred_translation"] = _pick_translation(copy.get("translations") or [], preferred_lang)
        chapters.append(copy)

    return {
        **chapter_payload,
        "chapters": chapters,
    }


async def get_chapter_list(title_id: str, lang: str | None = None) -> dict[str, Any]:
    title_id = _extract_title_id(title_id) or _clean(title_id)
    if not title_id:
        raise ValueError("title_id invalido para listar capitulos.")

    lang = _clean(lang).lower() or ""
    cache_key = _chapter_list_cache_key(title_id)

    async def _load():
        payload = _chapter_list_payload(title_id)

        referer = _TITLE_URL_CACHE.get(title_id) or _absolute_url(f"/title-detail/{title_id}/")
        first_error: Exception | None = None
        try:
            response = await asyncio.wait_for(
                _request_form_json_cached_quick(
                    "/api/v1/chapter/chapter-listing-by-title-id/",
                    payload,
                ),
                timeout=3.0,
            )
        except Exception as error:
            first_error = error
            try:
                response = await _request_form_json(
                    "/api/v1/chapter/chapter-listing-by-title-id/",
                    payload,
                )
            except Exception as full_error:
                try:
                    response = await _request_form_json_via_playwright(
                        "/api/v1/chapter/chapter-listing-by-title-id/",
                        payload,
                        referer,
                    )
                except Exception as playwright_error:
                    print("[CATALOG][CHAPTERS]", title_id, repr(first_error), repr(full_error), repr(playwright_error))
                    return {
                        "title_id": title_id,
                        "chapters": [],
                        "languages": [],
                        "total_translations": 0,
                        "partial": True,
                        "error": repr(first_error),
                    }

        if response.get("code") != 200:
            print("[CATALOG][CHAPTERS_STATUS]", title_id, response.get("code"))
            return {"title_id": title_id, "chapters": [], "languages": [], "total_translations": 0, "partial": True}

        chapters = _normalize_chapter_groups(response.get("ALL_CHAPTERS") or [], lang or PREFERRED_CHAPTER_LANG)
        languages = response.get("ALL_LANGUAGES") or []
        total_translations = int(response.get("TOTAL_TRANSLATIONS") or 0)
        _remember_title_summary(
            {
                "title_id": title_id,
                "languages": languages,
                "total_translations": total_translations,
                "total_chapters": len(chapters),
            }
        )
        return {
            "title_id": title_id,
            "chapters": chapters,
            "languages": languages,
            "total_translations": total_translations,
        }

    try:
        result = await _dedup_fetch(cache_key, CHAPTERS_TTL, _load)
        if isinstance(result, dict) and result.get("partial"):
            _CACHE.pop(cache_key, None)
        if isinstance(result, dict):
            return _chapter_payload_with_preferred_language(result, lang)
        return result
    except Exception as error:
        print("[CATALOG][CHAPTERS_UNHANDLED]", title_id, repr(error))
        return {
            "title_id": title_id,
            "chapters": [],
            "languages": [],
            "total_translations": 0,
            "partial": True,
            "error": repr(error),
        }


async def get_chapter_list_fast(title_id: str, lang: str | None = None) -> dict[str, Any]:
    title_id = _extract_title_id(title_id) or _clean(title_id)
    if not title_id:
        raise ValueError("title_id invalido para listar capitulos.")

    lang = _clean(lang).lower() or ""
    full_cache_key = _chapter_list_cache_key(title_id)
    cached = _cache_get(full_cache_key, CHAPTERS_TTL)
    if isinstance(cached, dict) and not cached.get("partial"):
        return _chapter_payload_with_preferred_language(cached, lang)

    cache_key = f"chapter-list-fast:{title_id}:all"

    async def _load():
        payload = _chapter_list_payload(title_id)

        try:
            response = await asyncio.wait_for(
                _request_form_json_quick(
                    "/api/v1/chapter/chapter-listing-by-title-id/",
                    payload,
                ),
                timeout=CHAPTER_LIST_QUICK_TIMEOUT,
            )
        except Exception as error:
            return {
                "title_id": title_id,
                "chapters": [],
                "languages": [],
                "total_translations": 0,
                "partial": True,
                "error": repr(error),
            }

        if response.get("code") != 200:
            return {
                "title_id": title_id,
                "chapters": [],
                "languages": [],
                "total_translations": 0,
                "partial": True,
                "error": f"status:{response.get('code')}",
            }

        chapters = _normalize_chapter_groups(response.get("ALL_CHAPTERS") or [], lang or PREFERRED_CHAPTER_LANG)
        languages = response.get("ALL_LANGUAGES") or []
        total_translations = int(response.get("TOTAL_TRANSLATIONS") or 0)
        _remember_title_summary(
            {
                "title_id": title_id,
                "languages": languages,
                "total_translations": total_translations,
                "total_chapters": len(chapters),
            }
        )
        return {
            "title_id": title_id,
            "chapters": chapters,
            "languages": languages,
            "total_translations": total_translations,
        }

    result = await _dedup_fetch(cache_key, min(CHAPTERS_TTL, 300), _load)
    if isinstance(result, dict) and result.get("partial"):
        _CACHE.pop(cache_key, None)
    elif isinstance(result, dict):
        _cache_set(full_cache_key, result)
        return _chapter_payload_with_preferred_language(result, lang)
    return result


def flatten_chapters(chapter_payload: dict[str, Any], preferred_lang: str | None = None, *, ascending: bool = False) -> list[dict[str, Any]]:
    preferred_lang = _clean(preferred_lang).lower() or PREFERRED_CHAPTER_LANG
    items: list[dict[str, Any]] = []
    payload_title_id = _extract_title_id(chapter_payload.get("title_id")) or _clean(chapter_payload.get("title_id"))

    chapters = list(chapter_payload.get("chapters") or [])
    if ascending:
        chapters = list(reversed(chapters))

    for chapter in chapters:
        translation = next(
            (
                item
                for item in (chapter.get("translations") or [])
                if _clean(item.get("language")).lower() == preferred_lang
            ),
            None,
        )
        if not translation:
            continue

        items.append(
            {
                "chapter_id": translation["id"],
                "chapter_url": translation["url"],
                "title_id": payload_title_id,
                "chapter_number": chapter.get("chapter_number") or "",
                "chapter_number_float": chapter.get("chapter_number_float") or "",
                "chapter_language": translation.get("language") or "",
                "chapter_volume": translation.get("volume") or "",
                "group_name": translation.get("group_name") or "",
                "updated_at": translation.get("date") or "",
            }
        )
        _remember_chapter_title(translation["id"], payload_title_id)

    return items


def get_adjacent_chapters(
    chapter_payload: dict[str, Any],
    chapter_id: str,
    preferred_lang: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    flattened = flatten_chapters(chapter_payload, preferred_lang, ascending=True)
    current_id = _extract_chapter_id(chapter_id) or _clean(chapter_id)

    for index, item in enumerate(flattened):
        if item["chapter_id"] != current_id:
            continue
        previous_item = flattened[index - 1] if index > 0 else None
        next_item = flattened[index + 1] if index + 1 < len(flattened) else None
        return previous_item, next_item
    return None, None


async def _resolve_title_url_from_id(title_id: str) -> str:
    title_id = _extract_title_id(title_id) or _clean(title_id)
    if not title_id:
        raise ValueError("title_id invalido.")

    cached = _TITLE_URL_CACHE.get(title_id)
    if cached and "/title-detail/" in cached:
        return cached
    if cached:
        _TITLE_URL_CACHE.pop(title_id, None)

    summary = get_cached_title_summary(title_id)
    summary_url = _absolute_url((summary or {}).get("url") or (summary or {}).get("title_url") or "")
    if summary_url and "/title-detail/" in summary_url:
        _remember_title_url(title_id, summary_url)
        return summary_url

    direct_candidate = _absolute_url(f"/title-detail/{title_id}/")
    direct_html = await _try_request_text(direct_candidate)
    if direct_html and _extract_title_id(direct_html) == title_id:
        _remember_title_url(title_id, direct_candidate)
        return direct_candidate

    chapter_payload = await get_chapter_list(title_id, PREFERRED_CHAPTER_LANG)
    sample_translation = next(
        (
            translation
            for chapter in chapter_payload.get("chapters") or []
            for translation in (chapter.get("translations") or [])
            if translation.get("url")
        ),
        None,
    )
    if sample_translation:
        chapter_html = await _request_text(sample_translation["url"])
        chapter_data = _parse_chapter_detail_html(chapter_html, sample_translation["url"])
        title_url = chapter_data.get("title_url") or ""
        if title_url:
            _remember_title_url(title_id, title_url)
            return title_url

    _remember_title_url(title_id, direct_candidate)
    return direct_candidate


async def get_title_details(title_ref: str) -> dict[str, Any]:
    title_ref = _clean(title_ref)
    if not title_ref:
        raise ValueError("Referencia de obra invalida.")

    title_id = _extract_title_id(title_ref)
    cache_key = f"title-details:{title_ref if '/title-detail/' in title_ref else title_id}"

    async def _load():
        if "/title-detail/" in title_ref:
            url = _absolute_url(title_ref)
        else:
            url = await _resolve_title_url_from_id(title_id or title_ref)

        html_text = await _request_text(url)
        details = _parse_title_detail_html(html_text, url)
        if not details.get("title_id") and title_id:
            details["title_id"] = title_id
            _remember_title_url(title_id, url)
        _remember_title_summary(details)
        return details

    return await _dedup_fetch(cache_key, TITLE_TTL, _load)


def _merge_title_metadata(details: dict[str, Any], anilist: dict[str, Any]) -> dict[str, Any]:
    if not anilist:
        return details

    merged = dict(details)

    genres = []
    seen_genres: set[str] = set()
    for raw in [*(details.get("genres") or []), *(anilist.get("anilist_genres") or [])]:
        genre = _clean(raw)
        normalized = genre.lower()
        if not genre or normalized in seen_genres:
            continue
        seen_genres.add(normalized)
        genres.append(genre)

    merged["alt_titles"] = []
    merged["genres"] = genres
    merged["anilist_id"] = anilist.get("anilist_id")
    merged["anilist_url"] = anilist.get("anilist_url") or ""
    merged["anilist_status"] = anilist.get("anilist_status") or ""
    merged["anilist_format"] = anilist.get("anilist_format") or ""
    merged["anilist_score"] = anilist.get("anilist_score") or 0
    merged["anilist_chapters"] = anilist.get("anilist_chapters") or 0
    merged["anilist_volumes"] = anilist.get("anilist_volumes") or 0
    merged["anilist_country"] = anilist.get("anilist_country") or ""
    merged["anilist_titles"] = anilist.get("anilist_titles") or []
    merged["cover_color"] = anilist.get("cover_color") or ""
    merged["banner_url"] = anilist.get("banner_url") or merged.get("background_url") or merged.get("cover_url") or ""
    if not merged.get("background_url"):
        merged["background_url"] = merged["banner_url"]
    if not merged.get("cover_url") and anilist.get("cover_url_anilist"):
        merged["cover_url"] = anilist["cover_url_anilist"]
    if not merged.get("description") and anilist.get("anilist_description"):
        merged["description"] = anilist["anilist_description"]
    if not merged.get("status") and anilist.get("anilist_status"):
        merged["status"] = anilist["anilist_status"]
    if not merged.get("rating") and anilist.get("anilist_score"):
        merged["rating"] = str(anilist["anilist_score"])
    return merged


async def _resolve_title_id_for_chapter(chapter: dict[str, Any], title_hint: str = "") -> str:
    title_id = (
        _extract_title_id(chapter.get("title_id"))
        or _extract_title_id(chapter.get("title_url"))
        or _extract_title_id(title_hint)
        or _CHAPTER_TITLE_CACHE.get(_extract_chapter_id(chapter.get("chapter_id")) or "")
    )
    if title_id:
        _remember_chapter_title(chapter.get("chapter_id") or "", title_id)
        return title_id

    title_name = _clean(chapter.get("title"))
    if not title_name:
        return ""

    try:
        candidates = await search_titles(title_name, limit=5)
    except Exception:
        return ""

    normalized_title = _normalize_text(title_name)
    for item in candidates:
        candidate_title = _normalize_text(item.get("title") or "")
        candidate_id = _extract_title_id(item.get("title_id") or "")
        if candidate_id and candidate_title == normalized_title:
            _remember_chapter_title(chapter.get("chapter_id") or "", candidate_id)
            return candidate_id

    for item in candidates:
        candidate_id = _extract_title_id(item.get("title_id") or "")
        if candidate_id:
            _remember_chapter_title(chapter.get("chapter_id") or "", candidate_id)
            return candidate_id

    return ""


async def get_title_chapters_snapshot(title_ref: str, lang: str | None = None) -> dict[str, Any]:
    resolved_lang = lang or PREFERRED_CHAPTER_LANG
    title_ref = _clean(title_ref)
    title_id = _extract_title_id(title_ref) or title_ref
    if not title_id:
        raise ValueError("Referencia de obra invalida.")

    cached = get_cached_title_bundle(title_id, resolved_lang)
    if cached is not None and cached.get("chapters"):
        return cached

    summary = get_cached_title_summary(title_id) or {}
    chapters_payload = await get_chapter_list_fast(title_id, resolved_lang)
    chapters = chapters_payload.get("chapters") or []
    latest = flatten_chapters(chapters_payload, resolved_lang)
    total_chapters = len(chapters) or _chapter_count_from_summary(summary)
    title = summary.get("display_title") or summary.get("title") or "Manga"
    cover_url = summary.get("cover_url") or ""

    bundle = {
        "title_id": title_id,
        "title": title,
        "display_title": title,
        "cover_url": cover_url,
        "background_url": summary.get("background_url") or cover_url,
        "status": summary.get("status") or summary.get("anilist_status") or "carregando",
        "rating": summary.get("rating") or summary.get("anilist_score") or "",
        "genres": summary.get("genres") or summary.get("anilist_genres") or [],
        "chapters": chapters,
        "languages": chapters_payload.get("languages") or [],
        "total_chapters": total_chapters,
        "latest_chapter": latest[0] if latest else summary.get("latest_chapter"),
        "chapters_partial": bool(chapters_payload.get("partial")) or not bool(chapters),
        "chapters_error": chapters_payload.get("error") or "",
        "metadata_partial": True,
    }

    summary_payload = dict(bundle)
    if title == "Manga" and not (summary.get("display_title") or summary.get("title")):
        summary_payload.pop("title", None)
        summary_payload.pop("display_title", None)
        summary_payload.pop("status", None)
    _remember_title_summary(summary_payload)
    if chapters and not bundle["chapters_partial"]:
        _cache_set(_title_bundle_cache_key(title_id, resolved_lang), bundle)
    return bundle


async def get_title_bundle(title_ref: str, lang: str | None = None) -> dict[str, Any]:
    resolved_lang = lang or PREFERRED_CHAPTER_LANG
    title_ref = _clean(title_ref)
    title_id = _extract_title_id(title_ref)
    cache_key = _title_bundle_cache_key(title_ref, resolved_lang)
    cached = _cache_get(cache_key, BUNDLE_TTL)
    if isinstance(cached, dict) and cached.get("metadata_partial"):
        _CACHE.pop(cache_key, None)

    async def _load():
        chapter_task = asyncio.create_task(get_chapter_list(title_id, resolved_lang)) if title_id else None
        try:
            details = await get_title_details(title_ref)
        except Exception:
            if chapter_task is not None:
                chapter_task.cancel()
            raise
        details_title_id = _extract_title_id(details.get("title_id")) or ""

        if chapter_task is not None and details_title_id == title_id:
            chapters_source = chapter_task
        else:
            if chapter_task is not None:
                chapter_task.cancel()
            chapters_source = get_chapter_list(details["title_id"], resolved_lang)

        chapters_payload, anilist = await asyncio.gather(
            chapters_source,
            enrich_title_metadata(details.get("title") or "", details.get("alt_titles") or []),
        )
        merged = _merge_title_metadata(details, anilist)
        merged["chapters"] = chapters_payload["chapters"]
        merged["languages"] = chapters_payload["languages"]
        source_total_chapters = details.get("source_total_chapters")
        merged["source_total_chapters"] = source_total_chapters
        merged["total_chapters"] = len(chapters_payload["chapters"])
        merged["chapters_partial"] = bool(chapters_payload.get("partial"))
        if not merged["chapters"] and source_total_chapters == 0:
            merged["chapters_partial"] = False
        merged["chapters_error"] = chapters_payload.get("error") or ""
        latest = flatten_chapters(chapters_payload, resolved_lang)
        merged["latest_chapter"] = latest[0] if latest else None
        _remember_title_summary(merged)
        return merged

    result = await _dedup_fetch(cache_key, BUNDLE_TTL, _load)
    if isinstance(result, dict) and result.get("chapters_partial"):
        _CACHE.pop(cache_key, None)
    return result


async def get_title_overview(title_ref: str) -> dict[str, Any]:
    title_ref = _clean(title_ref)
    title_id = _extract_title_id(title_ref)
    cache_key = f"title-overview:{title_ref if '/title-detail/' in title_ref else title_id or title_ref}"

    async def _load():
        details = await get_title_details(title_ref)
        anilist = await enrich_title_metadata(details.get("title") or "", [])
        merged = _merge_title_metadata(details, anilist)
        merged.setdefault("chapters", [])
        merged.setdefault("languages", [])
        merged.setdefault("total_chapters", 0)
        merged.setdefault("latest_chapter", None)
        _remember_title_summary(merged)
        return merged

    return await _dedup_fetch(cache_key, TITLE_TTL, _load)


def get_cached_title_overview(title_ref: str) -> dict[str, Any] | None:
    title_ref = _clean(title_ref)
    title_id = _extract_title_id(title_ref)
    cache_key = f"title-overview:{title_ref if '/title-detail/' in title_ref else title_id or title_ref}"
    cached = _cache_get(cache_key, TITLE_TTL)
    if cached is None:
        return None
    return dict(cached)


def get_cached_title_bundle(title_ref: str, lang: str | None = None) -> dict[str, Any] | None:
    resolved_lang = lang or PREFERRED_CHAPTER_LANG
    title_ref = _clean(title_ref)
    cache_key = _title_bundle_cache_key(title_ref, resolved_lang)
    cached = _cache_get(cache_key, BUNDLE_TTL)
    if cached is None:
        return None
    if isinstance(cached, dict) and cached.get("chapters_partial"):
        _CACHE.pop(cache_key, None)
        return None
    return dict(cached)


async def get_chapter_details(chapter_ref: str) -> dict[str, Any]:
    chapter_ref = _clean(chapter_ref)
    if not chapter_ref:
        raise ValueError("Referencia de capitulo invalida.")

    chapter_id = _extract_chapter_id(chapter_ref)
    cache_key = f"chapter-details:{chapter_ref if '/chapter-detail/' in chapter_ref else chapter_id}"

    async def _load():
        if "/chapter-detail/" in chapter_ref:
            url = _absolute_url(chapter_ref)
        else:
            url = _CHAPTER_URL_CACHE.get(chapter_id) or _absolute_url(f"/chapter-detail/{chapter_id}/")

        html_text = await _request_text(url)
        details = _parse_chapter_detail_html(html_text, url)
        if not details.get("chapter_id") and chapter_id:
            details["chapter_id"] = chapter_id
        return details

    return await _dedup_fetch(cache_key, CHAPTER_TTL, _load)


async def get_chapter_reader_payload(chapter_ref: str, lang: str | None = None, title_hint: str = "") -> dict[str, Any]:
    resolved_lang = lang or PREFERRED_CHAPTER_LANG
    chapter_ref = _clean(chapter_ref)
    chapter_id = _extract_chapter_id(chapter_ref)
    cache_key = f"reader:{chapter_ref if '/chapter-detail/' in chapter_ref else chapter_id or chapter_ref}:{resolved_lang}"

    async def _load():
        chapter = await get_chapter_details(chapter_ref)
        if not _extract_title_id(chapter.get("title_id")):
            chapter["title_id"] = await _resolve_title_id_for_chapter(chapter, title_hint)
        if not _extract_title_id(chapter.get("title_id")):
            raise RuntimeError("Nao consegui vincular esse capitulo a obra principal.")
        _remember_chapter_title(chapter.get("chapter_id") or "", chapter.get("title_id") or "")
        chapter_payload = await get_chapter_list(
            chapter["title_id"],
            resolved_lang or chapter.get("chapter_language") or PREFERRED_CHAPTER_LANG,
        )
        preferred_lang = chapter.get("chapter_language") or resolved_lang or PREFERRED_CHAPTER_LANG
        previous_chapter, next_chapter = get_adjacent_chapters(
            chapter_payload,
            chapter["chapter_id"],
            preferred_lang,
        )

        return {
            **chapter,
            "previous_chapter": previous_chapter,
            "next_chapter": next_chapter,
            "total_chapters": len(chapter_payload.get("chapters") or []),
        }

    return await _dedup_fetch(cache_key, READER_TTL, _load)


def get_cached_chapter_reader_payload(chapter_ref: str, lang: str | None = None, title_hint: str = "") -> dict[str, Any] | None:
    resolved_lang = lang or PREFERRED_CHAPTER_LANG
    chapter_ref = _clean(chapter_ref)
    chapter_id = _extract_chapter_id(chapter_ref)
    cache_key = f"reader:{chapter_ref if '/chapter-detail/' in chapter_ref else chapter_id or chapter_ref}:{resolved_lang}"
    cached = _cache_get(cache_key, READER_TTL)
    if cached is None:
        return None
    if title_hint:
        _remember_chapter_title(chapter_id or chapter_ref, title_hint)
    return dict(cached)


async def get_recent_chapter_updates(limit: int = HOME_SECTION_LIMIT) -> list[dict[str, Any]]:
    target_limit = max(1, int(limit))
    raw_items = await get_title_search(
        "getRecentlyUpdatedChapter",
        limit=max(24, target_limit * 3),
        page=1,
    )

    def _is_ptbr_or_unknown(value: Any) -> bool:
        normalized = _clean(value).lower().replace("_", "-")
        return normalized in {"", "pt-br", "ptbr", "pt"}

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_items:
        if not _is_ptbr_or_unknown(item.get("language")):
            continue
        chapter_id = item.get("chapter_id") or ""
        if not chapter_id:
            continue
        title_id = item.get("title_id") or ""
        key = f"{title_id}:{chapter_id}" if title_id else chapter_id
        if key in seen:
            continue
        seen.add(key)
        item["language"] = item.get("language") or "pt-br"
        _remember_chapter_title(chapter_id, title_id)
        results.append(item)
        if len(results) >= target_limit:
            break
    return results


async def get_home_payload(limit: int = HOME_SECTION_LIMIT) -> dict[str, Any]:
    limit = max(4, int(limit))

    featured = await get_title_search("getFeatured", limit=min(limit, 10))
    recommended = await get_title_search("getRecommend", limit=limit)
    top_viewed = await get_title_search("getRecentRead", limit=limit, search_time=RECENT_CHAPTER_TIME)
    latest_updates = await get_recent_chapter_updates(limit=max(limit, 12))
    recent_chapter_read = await get_title_search("getRecentChapterRead", limit=limit, search_time=RECENT_CHAPTER_TIME)
    popular_season = await get_title_search("getPopular", limit=limit)

    return {
        "featured": featured,
        "recommended": recommended,
        "top_viewed": top_viewed,
        "latest_updates": latest_updates,
        "recent_chapter_read": recent_chapter_read,
        "popular_season": popular_season,
        # Backward-compatible aliases used by older miniapp builds.
        "popular": top_viewed,
        "recent_titles": recent_chapter_read,
        "latest_titles": latest_updates,
        "recent_chapters": latest_updates,
    }


def get_cached_home_snapshot(limit: int = HOME_SECTION_LIMIT) -> dict[str, Any]:
    limit = max(4, int(limit))
    featured = get_cached_title_search("getFeatured", limit=min(limit, 10))
    recommended = get_cached_title_search("getRecommend", limit=limit)
    top_viewed = get_cached_title_search("getRecentRead", limit=limit, search_time=RECENT_CHAPTER_TIME)
    recent_chapter_read = get_cached_title_search("getRecentChapterRead", limit=limit, search_time=RECENT_CHAPTER_TIME)
    popular_season = get_cached_title_search("getPopular", limit=limit)
    return {
        "featured": featured,
        "recommended": recommended,
        "top_viewed": top_viewed,
        "latest_updates": [],
        "recent_chapter_read": recent_chapter_read,
        "popular_season": popular_season,
        "popular": top_viewed,
        "recent_titles": recent_chapter_read,
        "latest_titles": [],
        "recent_chapters": [],
    }


async def get_recent_chapters(limit: int = AUTO_POST_LIMIT) -> list[dict[str, Any]]:
    target_limit = max(1, int(limit))
    batch_size = max(24, target_limit * 4)
    max_pages = max(3, min(10, (target_limit // 6) + 3))

    def _normalize_lang(value: Any) -> str:
        return _clean(value).lower().replace("_", "-")

    def _is_ptbr_lang(value: Any) -> bool:
        normalized = _normalize_lang(value)
        return normalized in {"pt-br", "ptbr", "pt"}

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        raw_items = await get_title_search(
            "getRecentlyUpdatedChapter",
            limit=batch_size,
            page=page,
        )
        if not raw_items:
            break

        for item in raw_items:
            title_id = item.get("title_id") or ""
            chapter_id = item.get("chapter_id") or ""
            chapter_url = item.get("chapter_url") or ""
            title_url = item.get("url") or ""
            chapter_number = item.get("latest_chapter") or item.get("chapter_number") or ""
            chapter_data: dict[str, Any] | None = None

            language = _normalize_lang(item.get("language"))
            needs_detail_lookup = not _is_ptbr_lang(language)
            if language and not _is_ptbr_lang(language):
                continue

            if (not chapter_id or not chapter_number or not title_id or not title_url or needs_detail_lookup) and (chapter_url or chapter_id):
                try:
                    chapter_data = await get_chapter_details(chapter_url or chapter_id)
                except Exception:
                    chapter_data = None

            if needs_detail_lookup:
                detail_lang = _normalize_lang((chapter_data or {}).get("chapter_language"))
                if not _is_ptbr_lang(detail_lang):
                    continue
                language = detail_lang or "pt-br"
            elif not language:
                language = "pt-br"

            if chapter_data:
                chapter_id = chapter_id or chapter_data.get("chapter_id") or ""
                chapter_url = chapter_url or chapter_data.get("chapter_url") or ""
                chapter_number = chapter_number or chapter_data.get("chapter_number") or ""
                title_id = title_id or chapter_data.get("title_id") or ""
                title_url = title_url or chapter_data.get("title_url") or ""
                if not item.get("title"):
                    item["title"] = chapter_data.get("title") or item.get("title")
                item["cover_url"] = item.get("cover_url") or chapter_data.get("cover_url") or item.get("cover_url") or ""

            if not chapter_id and title_id:
                try:
                    chapter_payload = await get_chapter_list(title_id, "pt-br")
                except Exception:
                    continue
                latest = flatten_chapters(chapter_payload, "pt-br")
                if latest:
                    chapter_id = latest[0]["chapter_id"]
                    chapter_url = latest[0]["chapter_url"]
                    chapter_number = chapter_number or latest[0]["chapter_number"]

            if not chapter_id:
                continue

            _remember_chapter_title(chapter_id, title_id)

            key = f"{title_id}:{chapter_id}" if title_id else chapter_id
            if key in seen:
                continue
            seen.add(key)

            title = item.get("title") or item.get("display_title") or "Manga"
            display_title = item.get("display_title") or title

            results.append(
                {
                    "title_id": title_id,
                    "title": title,
                    "display_title": display_title,
                    "cover_url": item.get("cover_url") or "",
                    "background_url": item.get("background_url") or item.get("cover_url") or "",
                    "status": item.get("status") or "",
                    "updated_at": item.get("updated_at") or "",
                    "chapter_id": chapter_id,
                    "chapter_url": chapter_url,
                    "chapter_number": chapter_number,
                    "language": language or "pt-br",
                    "url": title_url,
                }
            )

            if len(results) >= target_limit:
                return results[:target_limit]

        if len(raw_items) < batch_size:
            break

    return results[:target_limit]


async def warm_catalog_cache(*, include_home: bool = True) -> None:
    if not BASE_URL:
        return

    try:
        await _ensure_browser_session()
    except Exception:
        pass

    if include_home:
        try:
            await get_title_search("getFeatured", limit=6)
            await get_title_search("getRecommend", limit=6)
            await get_title_search("getPopular", limit=6)
            await get_title_search("getRecentRead", limit=6, search_time=RECENT_CHAPTER_TIME)
            await get_title_search("getRecentChapterRead", limit=6, search_time=RECENT_CHAPTER_TIME)
            await get_recent_chapter_updates(limit=6)
        except Exception:
            pass


def schedule_warm_catalog_cache() -> asyncio.Task | None:
    global _WARMUP_TASK
    if _WARMUP_TASK and not _WARMUP_TASK.done():
        return _WARMUP_TASK

    try:
        _WARMUP_TASK = asyncio.create_task(warm_catalog_cache())
    except RuntimeError:
        _WARMUP_TASK = None
    return _WARMUP_TASK


def prefetch_title_bundles(title_refs: list[str], *, lang: str | None = None, limit: int = 3) -> asyncio.Task | None:
    refs = [(_clean(item)) for item in title_refs if _clean(item)]
    refs = refs[: max(0, limit)]
    if not refs:
        return None

    async def _runner():
        await asyncio.gather(*(get_title_chapters_snapshot(ref, lang) for ref in refs), return_exceptions=True)
        await asyncio.gather(*(get_title_bundle(ref, lang) for ref in refs), return_exceptions=True)

    try:
        return asyncio.create_task(_runner())
    except RuntimeError:
        return None


def prefetch_reader_payloads(chapter_refs: list[str], *, lang: str | None = None, limit: int = 3) -> asyncio.Task | None:
    refs = [(_clean(item)) for item in chapter_refs if _clean(item)]
    refs = refs[: max(0, limit)]
    if not refs:
        return None

    async def _runner():
        await asyncio.gather(*(get_chapter_reader_payload(ref, lang) for ref in refs), return_exceptions=True)

    try:
        return asyncio.create_task(_runner())
    except RuntimeError:
        return None
