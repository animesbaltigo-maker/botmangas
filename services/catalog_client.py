from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import time
import unicodedata
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

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
    CATALOG_COOKIE_HEADER,
    CATALOG_SITE_BASE,
    CATALOG_USER_AGENT,
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
DEFAULT_CATALOG_USER_AGENT = (
    CATALOG_USER_AGENT
    or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_HTTP_SEMAPHORE = asyncio.Semaphore(24)
_SOURCE_REQUEST_LOCK = asyncio.Lock()
_SOURCE_LAST_REQUEST_AT = 0.0

_CACHE: dict[str, dict[str, Any]] = {}
_INFLIGHT: dict[str, asyncio.Task] = {}
try:
    CATALOG_MEMORY_CACHE_LIMIT = max(200, int(os.getenv("CATALOG_MEMORY_CACHE_LIMIT", "900") or 900))
except ValueError:
    CATALOG_MEMORY_CACHE_LIMIT = 900
_TITLE_SEARCH_LOCK = asyncio.Lock()
_TITLE_URL_CACHE: dict[str, str] = {}
_TITLE_SUMMARY_CACHE: dict[str, dict[str, Any]] = {}
_TITLE_SUMMARY_CACHE_LOADED = False
_TITLE_SUMMARY_CACHE_SAVED_AT = 0.0
_CHAPTER_URL_CACHE: dict[str, str] = {}
_CHAPTER_TITLE_CACHE: dict[str, str] = {}
_CSRF_TOKEN: dict[str, Any] = {"value": "", "expires_at": 0.0}
_BROWSER_SESSION: dict[str, Any] = {"cookies": {}, "csrf_token": "", "expires_at": 0.0}
_BROWSER_SESSION_LOCK = asyncio.Lock()
_PLAYWRIGHT_FALLBACK_LOCK = asyncio.Lock()
_PLAYWRIGHT_FAIL_UNTIL = 0.0
_PLAYWRIGHT_LAST_CLEANUP = 0.0
_WARMUP_TASK: asyncio.Task | None = None

PLAYWRIGHT_SESSION_TTL = 1800
PLAYWRIGHT_NAV_TIMEOUT = 30000
PLAYWRIGHT_META_TIMEOUT = 10000
PLAYWRIGHT_CLEANUP_MAX_AGE_SECONDS = max(300, int(os.getenv("CATALOG_PLAYWRIGHT_CLEANUP_MAX_AGE", "900") or 900))
PLAYWRIGHT_COOLDOWN_SECONDS = max(60, int(os.getenv("CATALOG_PLAYWRIGHT_COOLDOWN", "300") or 300))
PLAYWRIGHT_FALLBACK_ENABLED = os.getenv("CATALOG_PLAYWRIGHT_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
SEARCH_REMOTE_TIMEOUT = 8.0
SEARCH_QUICK_TIMEOUT = 3.4
SEARCH_RICH_TIMEOUT = 4.6
SEARCH_MIN_REMOTE_RESULTS = 6
SEARCH_CACHE_VERSION = "v2"
LOCAL_SEARCH_SEED_TTL = 300
FORM_QUICK_TIMEOUT = httpx.Timeout(5.5, connect=4.0, read=5.5, write=5.5, pool=5.5)
CHAPTER_LIST_QUICK_TIMEOUT = 4.2
CHAPTER_LIST_CACHED_TIMEOUT = httpx.Timeout(2.8, connect=2.0, read=2.8, write=2.8, pool=2.8)
try:
    SOURCE_MIN_INTERVAL_SECONDS = max(0.0, float(os.getenv("CATALOG_SOURCE_MIN_INTERVAL", "0.35") or 0.35))
except ValueError:
    SOURCE_MIN_INTERVAL_SECONDS = 0.35

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


def _manual_cookie_dict() -> dict[str, str]:
    cookies: dict[str, str] = {}
    for chunk in CATALOG_COOKIE_HEADER.split(";"):
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = _clean(name)
        value = _clean(value)
        if name and value:
            cookies[name] = value
    return cookies


def _merge_source_headers(headers: dict[str, str] | None = None) -> dict[str, str] | None:
    if not CATALOG_COOKIE_HEADER and not CATALOG_USER_AGENT:
        return headers
    merged = dict(headers or {})
    if CATALOG_COOKIE_HEADER:
        merged.setdefault("Cookie", CATALOG_COOKIE_HEADER)
    if CATALOG_USER_AGENT:
        merged["User-Agent"] = CATALOG_USER_AGENT
    return merged


async def _seed_playwright_context(context) -> None:
    cookies = _manual_cookie_dict()
    if not cookies:
        return
    parsed = urlparse(BASE_URL)
    domain = parsed.hostname or ""
    if not domain:
        return
    await context.add_cookies(
        [
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
                "httpOnly": False,
                "secure": parsed.scheme == "https",
                "sameSite": "Lax",
            }
            for name, value in cookies.items()
        ]
    )


async def _page_csrf_token(page) -> str:
    try:
        return _clean(
            await page.evaluate(
                "() => document.querySelector('meta[name=\"csrf-token\"]')"
                "?.getAttribute('content') || ''"
            )
        )
    except Exception:
        return ""


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
    now = time.time()
    _CACHE[key] = {"time": now, "data": data}
    overflow = len(_CACHE) - CATALOG_MEMORY_CACHE_LIMIT
    if overflow > 0:
        for old_key, _ in sorted(_CACHE.items(), key=lambda pair: pair[1].get("time", 0))[:overflow]:
            _CACHE.pop(old_key, None)
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
    absolute = urljoin(f"{BASE_URL}/", text)
    parsed = urlparse(absolute)
    base = urlparse(BASE_URL)
    if base.scheme == "https" and parsed.scheme == "http" and parsed.hostname == base.hostname:
        parsed = parsed._replace(scheme="https", netloc=base.netloc)
        return urlunparse(parsed)
    return absolute


def _is_source_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        base = urlparse(BASE_URL)
    except ValueError:
        return False
    return bool(parsed.hostname and base.hostname and parsed.hostname == base.hostname)


async def _source_request_gate(url: str) -> None:
    global _SOURCE_LAST_REQUEST_AT
    if not _is_source_url(url) or SOURCE_MIN_INTERVAL_SECONDS <= 0:
        return
    async with _SOURCE_REQUEST_LOCK:
        now = time.monotonic()
        wait_for = SOURCE_MIN_INTERVAL_SECONDS - (now - _SOURCE_LAST_REQUEST_AT)
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        _SOURCE_LAST_REQUEST_AT = time.monotonic()


def _normalize_text(value: str) -> str:
    value = _clean(value).lower().replace("×", " x ").replace("✕", " x ")
    value = unicodedata.normalize("NFKD", value)
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


def _source_temporarily_disabled(url: str) -> bool:
    if os.getenv("CATALOG_SOURCE_DISABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    return _is_source_url(url)


def _raise_if_source_disabled(url: str) -> None:
    if _source_temporarily_disabled(url):
        raise RuntimeError("Fonte temporariamente desativada: cookie cf_clearance invalido ou bloqueado pela Cloudflare.")


def _playwright_temporarily_disabled() -> bool:
    return time.monotonic() < _PLAYWRIGHT_FAIL_UNTIL


def _playwright_runtime_allowed() -> bool:
    return PLAYWRIGHT_FALLBACK_ENABLED and not _playwright_temporarily_disabled()


def _mark_playwright_failure(error: BaseException | None = None) -> None:
    global _PLAYWRIGHT_FAIL_UNTIL
    _PLAYWRIGHT_FAIL_UNTIL = time.monotonic() + PLAYWRIGHT_COOLDOWN_SECONDS
    if error is not None:
        print(f"[CATALOG][PLAYWRIGHT_COOLDOWN] {error!r}", flush=True)


def cleanup_stale_playwright_processes(*, max_age_seconds: int | None = None, force: bool = False) -> int:
    global _PLAYWRIGHT_LAST_CLEANUP
    if os.getenv("CATALOG_PLAYWRIGHT_JANITOR", "1").strip().lower() in {"0", "false", "no", "off"}:
        return 0
    now = time.monotonic()
    if not force and now - _PLAYWRIGHT_LAST_CLEANUP < 120:
        return 0
    _PLAYWRIGHT_LAST_CLEANUP = now

    max_age = max(60, int(max_age_seconds or PLAYWRIGHT_CLEANUP_MAX_AGE_SECONDS))
    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    except Exception:
        return 0

    killed = 0
    own_pid = os.getpid()
    project_root = str(Path(__file__).resolve().parents[1])
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        pid = int(proc_dir.name)
        if pid == own_pid:
            continue
        try:
            raw_cmd = (proc_dir / "cmdline").read_bytes()
            if not raw_cmd:
                continue
            cmdline = raw_cmd.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
            if "playwright/driver/node" not in cmdline and "playwright_chromiumdev_profile" not in cmdline:
                continue
            cwd = ""
            with contextlib.suppress(Exception):
                cwd = os.readlink(proc_dir / "cwd")
            if project_root not in cmdline and not cwd.startswith(project_root):
                continue
            stat = (proc_dir / "stat").read_text(encoding="utf-8", errors="ignore")
            start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
            age = uptime - (start_ticks / ticks)
            if age < max_age:
                continue
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            continue
        except Exception:
            continue
    if killed:
        print(f"[CATALOG][PLAYWRIGHT_JANITOR] killed={killed} max_age={max_age}", flush=True)
    return killed


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


def _normalize_language_code(value: Any) -> str:
    return _clean(value).lower().replace("_", "-")


def _is_ptbr_language(value: Any) -> bool:
    normalized = _normalize_language_code(value)
    return normalized in {"pt-br", "ptbr", "pt"}


def _language_from_flag_image(img: Any) -> str:
    src = _clean(img.get("src") if img else "").lower()
    alt = _normalize_language_code(img.get("alt") if img else "")
    title = _normalize_language_code(img.get("title") if img else "")

    for value in (alt, title):
        if value:
            return value

    match = re.search(r"/flags/([^/?#]+)\.(?:webp|png|jpg|jpeg|svg)", src, flags=re.I)
    if not match:
        return ""
    flag = match.group(1).lower()
    if flag == "br":
        return "pt-br"
    if flag == "gb":
        return "en"
    return flag


def _chapter_number_from_label(label: str) -> str:
    text = _clean(label)
    match = re.search(r"\bCh\.\s*([0-9]+(?:\.[0-9]+)?)", text, flags=re.I)
    if match:
        return match.group(1)
    match = re.search(r"\bCap[ií]tulo\s*([0-9]+(?:\.[0-9]+)?)", text, flags=re.I)
    if match:
        return match.group(1)
    return text


def _parse_recent_chapter_entries(raw_html: Any) -> list[dict[str, Any]]:
    html_text = str(raw_html or "").strip()
    if not html_text:
        return []

    soup = BeautifulSoup(html_text, "html.parser")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for anchor in soup.select("a[href*='/chapter-detail/']"):
        href = _absolute_url(anchor.get("href"))
        chapter_id = _extract_chapter_id(href)
        if not chapter_id or chapter_id in seen:
            continue

        row = anchor
        for _ in range(5):
            parent = row.find_parent("div")
            if parent is None:
                break
            row = parent
            if row.select_one("img[src*='/flags/']"):
                break

        flag_img = row.select_one("img[src*='/flags/']")
        language = _language_from_flag_image(flag_img)
        group_img = row.select_one("a[href*='/group/'] img")
        label = _clean(anchor.get_text(" ", strip=True))
        row_text = _clean(row.get_text(" ", strip=True))
        updated_at = ""
        time_match = re.search(r"\b(\d+\s*[smhdw]\s+ago|just now|agora h[aá]\s+pouco)\b", row_text, flags=re.I)
        if time_match:
            updated_at = _clean(time_match.group(1))

        seen.add(chapter_id)
        entries.append(
            {
                "chapter_id": chapter_id,
                "chapter_url": href,
                "chapter_label": label,
                "chapter_number": _chapter_number_from_label(label),
                "language": language,
                "language_flag": _absolute_url(flag_img.get("src") if flag_img else ""),
                "group_name": _clean((group_img.get("title") or group_img.get("alt")) if group_img else ""),
                "updated_at": updated_at,
            }
        )

    return entries


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
        "preferred_title",
        "alt_titles",
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
        try:
            if route.request.resource_type in {"image", "media", "font", "stylesheet"}:
                await route.abort()
                return
            await route.continue_()
        except Exception:
            return

    try:
        await context.route("**/*", _route)
    except Exception:
        pass

    navigation_error: Exception | None = None
    try:
        await page.goto(
            f"{BASE_URL}/",
            wait_until="domcontentloaded",
            timeout=PLAYWRIGHT_NAV_TIMEOUT,
        )
    except Exception as error:
        navigation_error = error

    deadline = time.monotonic() + (PLAYWRIGHT_META_TIMEOUT / 1000)
    while time.monotonic() < deadline:
        token = await _page_csrf_token(page)
        if token:
            return
        await page.wait_for_timeout(100)

    if navigation_error:
        raise RuntimeError(f"Falha ao abrir a fonte pelo navegador: {navigation_error!r}") from navigation_error


async def _ensure_browser_session(force_refresh: bool = False) -> dict[str, Any]:
    if _source_temporarily_disabled(BASE_URL):
        raise RuntimeError("Fonte temporariamente desativada: sessao de navegador bloqueada.")
    if not PLAYWRIGHT_FALLBACK_ENABLED:
        raise RuntimeError("Fallback Playwright desativado para manter a fonte rapida.")
    if _playwright_temporarily_disabled():
        raise RuntimeError("Sessao de navegador em cooldown apos falhas recentes.")
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

    cleanup_stale_playwright_processes()
    async with _BROWSER_SESSION_LOCK:
        if not force_refresh and _browser_session_is_valid():
            return {
                "cookies": dict(_BROWSER_SESSION["cookies"]),
                "csrf_token": _BROWSER_SESSION["csrf_token"],
                "expires_at": _BROWSER_SESSION["expires_at"],
            }

        playwright = None
        browser = None
        context = None
        page = None
        try:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(**_playwright_launch_kwargs())
            try:
                context = await browser.new_context(
                    user_agent=DEFAULT_CATALOG_USER_AGENT,
                    locale="pt-BR",
                )
                await _seed_playwright_context(context)
                page = await context.new_page()
                await _prepare_playwright_page(context, page)

                csrf_token = await _page_csrf_token(page)
                cookies = {
                    _clean(item.get("name")): _clean(item.get("value"))
                    for item in (await context.cookies())
                    if _clean(item.get("name")) and _clean(item.get("value"))
                }
            finally:
                for resource in (page, context, browser):
                    if resource is None:
                        continue
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(resource.close(), timeout=5)
        except Exception as error:
            _mark_playwright_failure(error)
            raise
        finally:
            if playwright is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(playwright.stop(), timeout=5)
            cleanup_stale_playwright_processes(max_age_seconds=30, force=True)

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
    session_cookies = dict((session or {}).get("cookies") or {})
    csrf_token = _clean((session or {}).get("csrf_token"))
    if not csrf_token:
        csrf_token = await get_csrf_token(allow_browser=allow_browser_token)

    referer = _absolute_url(referer) or f"{BASE_URL}/"
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "X-CSRF-TOKEN": csrf_token,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "Origin": BASE_URL,
    }
    if CATALOG_COOKIE_HEADER and not session_cookies:
        headers["Cookie"] = CATALOG_COOKIE_HEADER
    if CATALOG_USER_AGENT:
        headers["User-Agent"] = CATALOG_USER_AGENT
    return headers


async def _request_form_json_via_playwright(path: str, data: dict[str, Any], referer: str) -> dict[str, Any]:
    if _source_temporarily_disabled(_absolute_url(path)):
        raise RuntimeError("Fonte temporariamente desativada: fallback Playwright bloqueado.")
    if not PLAYWRIGHT_FALLBACK_ENABLED:
        raise RuntimeError("Fallback Playwright desativado para manter a fonte rapida.")
    if _playwright_temporarily_disabled():
        raise RuntimeError("Fallback Playwright em cooldown apos falhas recentes.")
    if async_playwright is None:
        raise RuntimeError(
            "Playwright nao esta instalado. Instale 'playwright' e rode "
            "'python -m playwright install chromium'."
        )

    url = _absolute_url(path)
    referer = _absolute_url(referer) or f"{BASE_URL}/"

    cleanup_stale_playwright_processes()
    async with _PLAYWRIGHT_FALLBACK_LOCK:
        playwright = None
        browser = None
        context = None
        page = None
        response_status = 0
        try:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(**_playwright_launch_kwargs())
            context = await browser.new_context(
                user_agent=DEFAULT_CATALOG_USER_AGENT,
                locale="pt-BR",
            )
            await _seed_playwright_context(context)
            page = await context.new_page()
            await _prepare_playwright_page(context, page)
            context_cookies = {
                _clean(item.get("name")): _clean(item.get("value"))
                for item in (await context.cookies())
                if _clean(item.get("name")) and _clean(item.get("value"))
            }
            headers = await _build_ajax_headers(
                {
                    "csrf_token": await _page_csrf_token(page),
                    "cookies": context_cookies,
                },
                referer,
            )
            response = await context.request.post(
                url,
                form=data,
                headers=headers,
                timeout=PLAYWRIGHT_NAV_TIMEOUT,
            )
            response_status = int(response.status)
            response_text = await response.text()
        except Exception as error:
            _mark_playwright_failure(error)
            raise
        finally:
            for resource in (page, context, browser):
                if resource is None:
                    continue
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(resource.close(), timeout=5)
            if playwright is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(playwright.stop(), timeout=5)
            cleanup_stale_playwright_processes(max_age_seconds=30, force=True)

    if response_status != 200:
        raise RuntimeError(f"Playwright recebeu HTTP {response_status} ao consultar {url}.")

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
    recent_chapters = _parse_recent_chapter_entries(item.get("last_chapter") or item.get("latest_chapters"))
    first_recent_chapter = recent_chapters[0] if recent_chapters else {}

    normalized = {
        "title_id": title_id,
        "chapter_id": chapter_id or first_recent_chapter.get("chapter_id") or "",
        "url": url,
        "chapter_url": chapter_url or first_recent_chapter.get("chapter_url") or "",
        "title": clean_title,
        "display_title": display_title or clean_title,
        "raw_title": _clean(raw_title),
        "cover_url": _absolute_url(item.get("cover") or item.get("img") or item.get("image") or item.get("thumbnail")),
        "background_url": _absolute_url(item.get("background") or item.get("cover") or item.get("img")),
        "status": _strip_html(item.get("status") or item.get("status_label") or item.get("statusText")),
        "rating": _clean(item.get("rating") or item.get("score")),
        "followers": _clean(item.get("followers") or item.get("bookmark")),
        "views": _clean(item.get("views")),
        "updated_at": _clean(item.get("updated_at") or item.get("updatedAt") or item.get("latest") or first_recent_chapter.get("updated_at")),
        "language": _normalize_language_code(item.get("language") or item.get("lang") or item.get("language_code") or first_recent_chapter.get("language")),
        "language_flag": _absolute_url(item.get("languageFlag") or item.get("flag") or first_recent_chapter.get("language_flag")),
        "latest_chapter": _clean(
            item.get("chapter")
            or item.get("latest_chapter")
            or item.get("updated_chapter")
            or item.get("chapter_number")
            or first_recent_chapter.get("chapter_number")
        ),
        "recent_chapters": recent_chapters,
        "adult": bool(item.get("isAdult") or item.get("adult") or item.get("is_adult")),
    }

    _remember_title_url(title_id, url)
    _remember_title_summary(normalized)
    if normalized["chapter_id"] and normalized["chapter_url"]:
        _remember_chapter_url(normalized["chapter_id"], normalized["chapter_url"])
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
    _raise_if_source_disabled(url)
    client = await get_http_client()
    last_error: Exception | None = None
    headers = _merge_source_headers(headers)

    for attempt in range(3):
        try:
            await _source_request_gate(url)
            async with _HTTP_SEMAPHORE:
                response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.text
        except (httpx.HTTPError, httpx.TimeoutException) as error:
            last_error = error
            await asyncio.sleep(0.35 * (attempt + 1))

    raise RuntimeError(f"Falha ao buscar {url}: {last_error!r}")


async def _request_text_fast(url: str, *, headers: dict[str, str] | None = None) -> str:
    _raise_if_source_disabled(url)
    client = await get_http_client()
    headers = _merge_source_headers(headers)

    await _source_request_gate(url)
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
    _raise_if_source_disabled(url)
    referer = _build_ajax_referer(path, data)

    last_error: Exception | None = None
    browser_error: Exception | None = None

    browser_session = _browser_session_snapshot()
    if not browser_session and _playwright_runtime_allowed():
        try:
            browser_session = await _ensure_browser_session(force_refresh=False)
        except Exception as error:
            browser_error = error
    headers = await _build_ajax_headers(
        browser_session,
        referer,
        allow_browser_token=bool(browser_session),
    )
    cookies = {**_manual_cookie_dict(), **dict((browser_session or {}).get("cookies") or {})}

    for attempt in range(3):
        try:
            await _source_request_gate(url)
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
                    cookies = {**_manual_cookie_dict(), **dict(browser_session.get("cookies") or {})}
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
                        cookies = {**_manual_cookie_dict(), **dict(browser_session.get("cookies") or {})}
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
    _raise_if_source_disabled(url)
    referer = _build_ajax_referer(path, data)
    headers = await _build_ajax_headers(None, referer, allow_browser_token=False)
    last_error: Exception | None = None

    for attempt in range(2):
        try:
            await _source_request_gate(url)
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
    _raise_if_source_disabled(url)
    referer = _absolute_url(_build_ajax_referer(path, data)) or f"{BASE_URL}/"
    csrf_token = ""
    cookies: dict[str, str] = {}

    if _CSRF_TOKEN["value"] and time.time() < _CSRF_TOKEN["expires_at"]:
        csrf_token = _clean(_CSRF_TOKEN["value"])
    elif _browser_session_is_valid():
        csrf_token = _clean(_BROWSER_SESSION.get("csrf_token"))
        cookies = {**_manual_cookie_dict(), **dict(_BROWSER_SESSION.get("cookies") or {})}
    else:
        cookies = _manual_cookie_dict()

    headers = {
        "Accept": "application/json,text/plain,*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "Origin": BASE_URL,
    }
    if csrf_token:
        headers["X-CSRF-TOKEN"] = csrf_token
    if CATALOG_COOKIE_HEADER:
        headers["Cookie"] = CATALOG_COOKIE_HEADER
    if CATALOG_USER_AGENT:
        headers["User-Agent"] = CATALOG_USER_AGENT

    await _source_request_gate(url)
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

    first_token_error: Exception | None = None
    try:
        token = await _token_from_html(fast=True)
        _CSRF_TOKEN["value"] = token
        _CSRF_TOKEN["expires_at"] = time.time() + 1800
        return token
    except Exception as error:
        first_token_error = error

    if allow_browser and PLAYWRIGHT_FALLBACK_ENABLED:
        try:
            browser_session = await _ensure_browser_session(force_refresh=force_refresh)
            token = _clean(browser_session.get("csrf_token"))
            if token:
                _CSRF_TOKEN["value"] = token
                _CSRF_TOKEN["expires_at"] = time.time() + 1800
                return token
        except Exception:
            pass

    if allow_browser and not PLAYWRIGHT_FALLBACK_ENABLED and first_token_error is not None:
        raise first_token_error

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

        async with _TITLE_SEARCH_LOCK:
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
        if quick_error is not None or rich_error is not None:
            schedule_warm_catalog_cache()
            return [], True
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
                if _is_auth_block_error(full_error):
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
                else:
                    print("[CATALOG][CHAPTERS]", title_id, repr(first_error), repr(full_error))
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


def flatten_chapters(chapter_payload: dict[str, Any] | list[Any], preferred_lang: str | None = None, *, ascending: bool = False) -> list[dict[str, Any]]:
    preferred_lang = _clean(preferred_lang).lower() or PREFERRED_CHAPTER_LANG
    items: list[dict[str, Any]] = []
    if isinstance(chapter_payload, list):
        chapter_payload = {"chapters": chapter_payload}
    if not isinstance(chapter_payload, dict):
        return items
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
        except Exception as error:
            if chapter_task is not None:
                chapter_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await chapter_task
            if title_id and _is_auth_block_error(error):
                return await get_title_chapters_snapshot(title_id, resolved_lang)
            raise
        details_title_id = _extract_title_id(details.get("title_id")) or ""

        if chapter_task is not None and details_title_id == title_id:
            chapters_source = chapter_task
        else:
            if chapter_task is not None:
                chapter_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await chapter_task
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


async def get_origin_titles(origin: str, limit: int = HOME_SECTION_LIMIT, page: int = 1) -> list[dict[str, Any]]:
    normalized_origin = _clean(origin).lower().replace("-", "").replace("_", "")
    origin_map = {
        "manga": "manga",
        "mangas": "manga",
        "manhwa": "manhwa",
        "manhwas": "manhwa",
        "manhua": "manhua",
        "manhuas": "manhua",
    }
    resolved = origin_map.get(normalized_origin, normalized_origin or "manga")
    target_limit = max(1, int(limit))
    page = max(1, int(page))

    payload_variants = [
        {"search_origin": resolved, "page": page},
        {"origin": resolved, "page": page},
        {"search_format": resolved, "page": page},
        {"format": resolved, "page": page},
    ]

    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for extra in payload_variants:
        items = await get_title_search("getByOrigin", limit=target_limit, **extra)
        for item in items:
            title_id = item.get("title_id") or ""
            if not title_id or title_id in seen:
                continue
            seen.add(title_id)
            item["origin"] = resolved
            results.append(item)
            if len(results) >= target_limit:
                return results
        if results:
            return results

    fallback_type = {
        "manga": "getFeatured",
        "manhwa": "getRecommend",
        "manhua": "getPopular",
    }.get(resolved, "getFeatured")
    return await get_title_search(fallback_type, limit=target_limit, page=page)


async def get_home_payload(limit: int = HOME_SECTION_LIMIT) -> dict[str, Any]:
    limit = max(4, int(limit))

    async def _safe(coro, fallback: list[dict[str, Any]] | None = None, timeout: float = 10.0) -> list[dict[str, Any]]:
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except Exception:
            return list(fallback or [])

    featured = await _safe(get_title_search("getFeatured", limit=min(limit, 10)), timeout=12.0)
    manga = await _safe(get_origin_titles("manga", limit=limit), timeout=12.0)
    manhwa = await _safe(get_origin_titles("manhwa", limit=limit), timeout=12.0)
    manhua = await _safe(get_origin_titles("manhua", limit=limit), timeout=12.0)
    recommended = await _safe(get_title_search("getRecommend", limit=limit), timeout=12.0)
    top_viewed = await _safe(get_title_search("getRecentRead", limit=limit, search_time=RECENT_CHAPTER_TIME), timeout=12.0)
    latest_updates = await _safe(get_recent_chapter_updates(limit=max(limit, 12)), timeout=12.0)
    recent_chapter_read = await _safe(get_title_search("getRecentChapterRead", limit=limit, search_time=RECENT_CHAPTER_TIME), timeout=12.0)
    popular_season = await _safe(get_title_search("getPopular", limit=limit), timeout=12.0)

    if not manga:
        manga = featured[:limit]
    if not manhwa:
        manhwa = recommended[:limit]
    if not manhua:
        manhua = popular_season[:limit]
    if not featured:
        featured = (manga or manhwa or manhua or popular_season or recommended)[: min(limit, 10)]

    return {
        "featured": featured,
        "manga": manga,
        "manhwa": manhwa,
        "manhua": manhua,
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
    manga = get_cached_title_search("getByOrigin", limit=limit, search_origin="manga", page=1)
    manhwa = get_cached_title_search("getByOrigin", limit=limit, search_origin="manhwa", page=1)
    manhua = get_cached_title_search("getByOrigin", limit=limit, search_origin="manhua", page=1)
    recommended = get_cached_title_search("getRecommend", limit=limit)
    top_viewed = get_cached_title_search("getRecentRead", limit=limit, search_time=RECENT_CHAPTER_TIME)
    recent_chapter_read = get_cached_title_search("getRecentChapterRead", limit=limit, search_time=RECENT_CHAPTER_TIME)
    popular_season = get_cached_title_search("getPopular", limit=limit)
    return {
        "featured": featured,
        "manga": manga,
        "manhwa": manhwa,
        "manhua": manhua,
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
    max_pages = max(10, min(25, (target_limit // 3) + 8))

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _is_ptbr_or_unknown(value: Any) -> bool:
        normalized = _clean(value).lower().replace("_", "-")
        return normalized in {"", "pt-br", "ptbr", "pt"}

    async def _genres_for_title(title_id: str) -> list[str]:
        if not title_id:
            return []

        summary = get_cached_title_summary(title_id)
        genres = []
        if isinstance(summary, dict):
            genres = summary.get("genres") or summary.get("anilist_genres") or []
        if genres:
            return list(genres)

        try:
            details = await asyncio.wait_for(get_title_details(title_id), timeout=10.0)
        except Exception:
            return []

        return list(details.get("genres") or details.get("anilist_genres") or [])

    for page in range(1, max_pages + 1):
        try:
            raw_items = await get_title_search(
                "getRecentlyUpdatedChapter",
                limit=batch_size,
                page=page,
            )
        except Exception as error:
            print("[CATALOG][RECENT_CHAPTERS]", repr(error))
            break
        if not raw_items:
            break

        for item in raw_items:
            title_id = item.get("title_id") or ""
            title_url = item.get("url") or ""
            direct_chapter_id = item.get("chapter_id") or ""
            recent_entries = item.get("recent_chapters") or []

            if direct_chapter_id and _is_ptbr_or_unknown(item.get("language")):
                chapter_number = item.get("latest_chapter") or item.get("chapter_number") or ""
                key = f"{title_id}:{direct_chapter_id}" if title_id else direct_chapter_id
                chapter_key = f"{title_id}:chapter:{chapter_number}" if title_id and chapter_number else ""
                if key in seen or (chapter_key and chapter_key in seen):
                    continue
                seen.add(key)
                if chapter_key:
                    seen.add(chapter_key)

                _remember_chapter_title(direct_chapter_id, title_id)
                title = item.get("title") or item.get("display_title") or "Manga"

                results.append(
                    {
                        "title_id": title_id,
                        "title": title,
                        "display_title": item.get("display_title") or title,
                        "cover_url": item.get("cover_url") or "",
                        "background_url": item.get("background_url") or item.get("cover_url") or "",
                        "status": item.get("status") or "",
                        "updated_at": item.get("updated_at") or "",
                        "chapter_id": direct_chapter_id,
                        "chapter_url": item.get("chapter_url") or "",
                        "chapter_number": chapter_number,
                        "language": item.get("language") or "pt-br",
                        "genres": await _genres_for_title(title_id),
                        "url": title_url,
                    }
                )

                if len(results) >= target_limit:
                    return results[:target_limit]

            pt_entries = [
                entry
                for entry in recent_entries
                if _is_ptbr_or_unknown(entry.get("language"))
            ]

            if recent_entries and not pt_entries:
                continue

            if pt_entries:
                candidate_entries = pt_entries
            else:
                continue

            for entry in candidate_entries:
                chapter_id = entry.get("chapter_id") or ""
                chapter_url = entry.get("chapter_url") or ""
                chapter_number = entry.get("chapter_number") or item.get("latest_chapter") or item.get("chapter_number") or ""
                if not chapter_id:
                    continue

                _remember_chapter_title(chapter_id, title_id)

                key = f"{title_id}:{chapter_id}" if title_id else chapter_id
                chapter_key = f"{title_id}:chapter:{chapter_number}" if title_id and chapter_number else ""
                if key in seen or (chapter_key and chapter_key in seen):
                    continue
                seen.add(key)
                if chapter_key:
                    seen.add(chapter_key)

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
                        "updated_at": entry.get("updated_at") or item.get("updated_at") or "",
                        "chapter_id": chapter_id,
                        "chapter_url": chapter_url,
                        "chapter_number": chapter_number,
                        "language": "pt-br",
                        "genres": await _genres_for_title(title_id),
                        "url": title_url,
                    }
                )

                if len(results) >= target_limit:
                    return results[:target_limit]

        if len(raw_items) < batch_size:
            break

    return results[:target_limit]


async def warm_catalog_cache(*, include_home: bool = True) -> None:
    if not BASE_URL or _source_temporarily_disabled(BASE_URL):
        return

    async def _with_timeout(coro, timeout: float = 20.0):
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except Exception:
            return None

    if PLAYWRIGHT_FALLBACK_ENABLED and os.getenv("CATALOG_WARM_BROWSER_SESSION", "").strip().lower() in {"1", "true", "yes", "on"}:
        await _with_timeout(_ensure_browser_session(), timeout=25.0)

    if include_home:
        await _with_timeout(get_title_search("getFeatured", limit=6))
        await _with_timeout(get_title_search("getPopular", limit=6))
        await _with_timeout(get_recent_chapter_updates(limit=6), timeout=30.0)


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
        for ref in refs:
            try:
                await asyncio.wait_for(get_title_chapters_snapshot(ref, lang), timeout=8.0)
            except Exception:
                pass

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
        for ref in refs:
            try:
                await asyncio.wait_for(get_chapter_reader_payload(ref, lang), timeout=8.0)
            except Exception:
                pass

    try:
        return asyncio.create_task(_runner())
    except RuntimeError:
        return None


_mangaball_clear_catalog_cache = clear_catalog_cache
_mangaball_get_csrf_token = get_csrf_token
_mangaball_get_search_fallback_titles = get_search_fallback_titles
_mangaball_get_cached_search_titles = get_cached_search_titles
_mangaball_search_titles = search_titles
_mangaball_search_titles_fast = search_titles_fast
_mangaball_get_title_search = get_title_search
_mangaball_get_cached_title_search = get_cached_title_search
_mangaball_get_origin_titles = get_origin_titles
_mangaball_get_home_payload = get_home_payload
_mangaball_get_cached_home_snapshot = get_cached_home_snapshot
_mangaball_get_recent_chapter_updates = get_recent_chapter_updates
_mangaball_get_recent_chapters = get_recent_chapters
_mangaball_get_cached_title_summary = get_cached_title_summary
_mangaball_get_title_details = get_title_details
_mangaball_get_title_overview = get_title_overview
_mangaball_get_title_chapters_snapshot = get_title_chapters_snapshot
_mangaball_get_title_bundle = get_title_bundle
_mangaball_get_cached_title_bundle = get_cached_title_bundle
_mangaball_get_chapter_list = get_chapter_list
_mangaball_get_chapter_list_fast = get_chapter_list_fast
_mangaball_get_cached_chapter_list = get_cached_chapter_list
_mangaball_flatten_chapters = flatten_chapters
_mangaball_get_adjacent_chapters = get_adjacent_chapters
_mangaball_get_chapter_details = get_chapter_details
_mangaball_get_chapter_reader_payload = get_chapter_reader_payload
_mangaball_get_cached_chapter_reader_payload = get_cached_chapter_reader_payload
_mangaball_warm_catalog_cache = warm_catalog_cache
_mangaball_schedule_warm_catalog_cache = schedule_warm_catalog_cache
_mangaball_prefetch_title_bundles = prefetch_title_bundles
_mangaball_prefetch_reader_payloads = prefetch_reader_payloads

_HYBRID_TITLE_MAP: dict[str, str] = {}
_HYBRID_MANGABALL_FAIL_UNTIL = 0.0
try:
    _HYBRID_MANGABALL_CONCURRENCY = max(1, int(os.getenv("HYBRID_MANGABALL_CONCURRENCY", "1") or 1))
except ValueError:
    _HYBRID_MANGABALL_CONCURRENCY = 1
_HYBRID_MANGABALL_SEMAPHORE = asyncio.Semaphore(_HYBRID_MANGABALL_CONCURRENCY)


def _hybrid_enabled() -> bool:
    return os.getenv("CATALOG_ACTIVE_SOURCE", "").strip().lower() in {"hybrid", "merged", "fusion"}


def _use_mangafire_source() -> bool:
    active = os.getenv("CATALOG_ACTIVE_SOURCE", "").strip().lower()
    return active == "mangafire" or (
        active not in {"hybrid", "merged", "fusion"}
        and "mangafire.to" in (CATALOG_SITE_BASE or "").lower()
    )


def _hybrid_mangaball_available() -> bool:
    if os.getenv("CATALOG_SOURCE_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    return time.monotonic() >= _HYBRID_MANGABALL_FAIL_UNTIL


def _hybrid_mark_mangaball_failure(error: BaseException | None = None, *, timeout: bool = False) -> None:
    global _HYBRID_MANGABALL_FAIL_UNTIL
    env_name = "HYBRID_MANGABALL_TIMEOUT_COOLDOWN" if timeout else "HYBRID_MANGABALL_COOLDOWN"
    default = "180" if timeout else "300"
    cooldown = float(os.getenv(env_name, default) or default)
    _HYBRID_MANGABALL_FAIL_UNTIL = max(_HYBRID_MANGABALL_FAIL_UNTIL, time.monotonic() + max(3.0, cooldown))
    if error is not None:
        tag = "HYBRID_MANGABALL_TIMEOUT" if timeout else "HYBRID_MANGABALL_COOLDOWN"
        print(f"[CATALOG][{tag}]", repr(error))


async def _hybrid_mangaball_call(label: str, producer, timeout: float):
    if not _hybrid_mangaball_available():
        raise RuntimeError("MangaBall temporariamente em cooldown.")
    try:
        async with _HYBRID_MANGABALL_SEMAPHORE:
            coro = producer() if callable(producer) else producer
            return await asyncio.wait_for(coro, timeout=max(0.4, float(timeout)))
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as error:
        _hybrid_mark_mangaball_failure(error, timeout=True)
        raise
    except BaseException as error:
        _hybrid_mark_mangaball_failure(error)
        raise


def _hybrid_partial_chapter_list(title_id: str, lang: str | None, error: BaseException | str) -> dict[str, Any]:
    return {
        "title_id": title_id,
        "source": "hybrid",
        "sources": ["fallback"],
        "chapters": [],
        "volumes": [],
        "languages": [_hybrid_translation_lang(lang or PREFERRED_CHAPTER_LANG)],
        "partial": True,
        "chapters_partial": True,
        "metadata_partial": True,
        "hybrid_secondary_partial": True,
        "error": str(error),
    }


def _hybrid_partial_title_bundle(title_id: str, lang: str | None, summary: dict[str, Any] | None, error: BaseException | str) -> dict[str, Any]:
    summary = dict(summary or {})
    title = (
        _clean(summary.get("display_title"))
        or _clean(summary.get("title"))
        or _clean(summary.get("preferred_title"))
        or "Manga"
    )
    cover_url = summary.get("cover_url") or ""
    return {
        "title_id": title_id,
        "title": title,
        "display_title": title,
        "cover_url": cover_url,
        "background_url": summary.get("background_url") or cover_url,
        "status": summary.get("status") or summary.get("anilist_status") or "carregando",
        "rating": summary.get("rating") or summary.get("anilist_score") or "",
        "genres": summary.get("genres") or summary.get("anilist_genres") or [],
        "chapters": [],
        "volumes": [],
        "languages": [_hybrid_translation_lang(lang or PREFERRED_CHAPTER_LANG)],
        "total_chapters": int(summary.get("total_chapters") or 0),
        "latest_chapter": summary.get("latest_chapter") or None,
        "source": "hybrid",
        "sources": ["fallback"],
        "chapters_partial": True,
        "metadata_partial": True,
        "hybrid_secondary_partial": True,
        "chapters_error": str(error),
    }


def _hybrid_real_score(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    try:
        number = Decimal(text.replace(",", ".").split("/", 1)[0])
    except Exception:
        return text
    if number <= 0:
        return ""
    if number == number.to_integral():
        return str(int(number))
    return format(number.normalize(), "f")


async def _hybrid_enrich_bundle_metadata(bundle: dict[str, Any], timeout: float = 2.5) -> dict[str, Any]:
    title = _clean(bundle.get("title") or bundle.get("display_title") or bundle.get("preferred_title"))
    if not title or title == "Manga":
        return bundle

    needs_score = not _hybrid_real_score(bundle.get("rating") or bundle.get("anilist_score"))
    needs_genres = not bool(bundle.get("genres") or bundle.get("anilist_genres"))
    needs_media = not bool(bundle.get("cover_url") and (bundle.get("background_url") or bundle.get("banner_url")))
    if not (needs_score or needs_genres or needs_media):
        return bundle

    try:
        anilist = await asyncio.wait_for(
            enrich_title_metadata(title, bundle.get("alt_titles") or []),
            timeout=max(0.5, timeout),
        )
    except Exception:
        return bundle
    if not anilist:
        return bundle

    enriched = _merge_title_metadata(bundle, anilist)
    score = _hybrid_real_score(enriched.get("rating") or enriched.get("anilist_score"))
    enriched["rating"] = score
    _remember_title_summary(enriched)
    return enriched


def _hybrid_summary_bundle_with_fallback(
    title_id: str,
    lang: str | None,
    summary: dict[str, Any] | None,
    fallback: dict[str, Any],
    error: BaseException | str,
) -> dict[str, Any]:
    base = _hybrid_partial_title_bundle(title_id, lang, summary, error)
    fallback = dict(fallback or {})
    fallback_sources = [
        str(source or "")
        for source in (fallback.get("sources") or [fallback.get("source") or "fallback"])
        if str(source or "")
    ]
    chapters = fallback.get("chapters") or []
    volumes = fallback.get("volumes") or []
    latest = _hybrid_flatten_chapters({"title_id": title_id, "chapters": chapters}, lang)
    sources = ["mangaball"]
    for source in fallback_sources:
        if source and source not in sources:
            sources.append(source)
    merged = {
        **base,
        "title_id": title_id,
        "source": "hybrid",
        "primary_source": "mangaball",
        "sources": sources,
        "chapters": chapters,
        "volumes": volumes,
        "languages": _hybrid_union_languages(base, fallback),
        "total_chapters": len(latest) or int(fallback.get("total_chapters") or base.get("total_chapters") or 0),
        "latest_chapter": latest[0] if latest else fallback.get("latest_chapter") or base.get("latest_chapter"),
        "chapters_partial": not bool(chapters),
        "metadata_partial": True,
        "hybrid_secondary_partial": False,
        "chapters_error": "",
        "mangaball_error": str(error),
        "source_ids": {
            **(fallback.get("source_ids") or {}),
            **{
                source: fallback.get("title_id")
                for source in fallback_sources
                if source and fallback.get("title_id")
            },
        },
    }
    _remember_title_summary(merged)
    return merged


def _hybrid_cached_mangaball_bundle(title_id: str, lang: str | None, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = dict(summary or {})
    cached = _mangaball_get_cached_title_bundle(title_id, lang)
    if isinstance(cached, dict) and cached.get("chapters"):
        cached = dict(cached)
        cached["source"] = "hybrid"
        cached["primary_source"] = "mangaball"
        cached["sources"] = ["mangaball"]
        return cached

    cached_chapters = _mangaball_get_cached_chapter_list(title_id, lang)
    if not isinstance(cached_chapters, dict) or not cached_chapters.get("chapters"):
        return {}

    base = _hybrid_partial_title_bundle(title_id, lang, summary or {"title_id": title_id}, "")
    chapters = cached_chapters.get("chapters") or []
    volumes = cached_chapters.get("volumes") or []
    flat = _hybrid_flatten_chapters({"title_id": title_id, "chapters": chapters}, lang)
    base.update(
        {
            "source": "hybrid",
            "primary_source": "mangaball",
            "sources": ["mangaball"],
            "chapters": chapters,
            "volumes": volumes,
            "languages": cached_chapters.get("languages") or base.get("languages") or [],
            "total_chapters": len(flat) or int(summary.get("total_chapters") or 0),
            "latest_chapter": flat[0] if flat else summary.get("latest_chapter"),
            "chapters_partial": False,
            "hybrid_secondary_partial": True,
            "chapters_error": "",
        }
    )
    return base


async def _hybrid_mangaball_chapter_list_bundle(title_id: str, lang: str | None, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = dict(summary or {})
    chapters_payload = await _hybrid_mangaball_call(
        "chapter_list_rescue",
        lambda: _mangaball_get_chapter_list(title_id, lang),
        float(os.getenv("HYBRID_MANGABALL_CHAPTER_RESCUE_TIMEOUT", "30.0") or 30.0),
    )
    base = _hybrid_partial_title_bundle(title_id, lang, summary or {"title_id": title_id}, "")
    chapters = _hybrid_merge_chapter_groups(chapters_payload.get("chapters") or [], [], lang)
    volumes = _hybrid_merge_chapter_groups(chapters_payload.get("volumes") or [], [], lang)
    flat = _hybrid_flatten_chapters({"title_id": title_id, "chapters": chapters}, lang)
    base.update(
        {
            "source": "hybrid",
            "primary_source": "mangaball",
            "sources": ["mangaball"],
            "chapters": chapters,
            "volumes": volumes,
            "languages": chapters_payload.get("languages") or base.get("languages") or [],
            "total_chapters": len(flat) or int(summary.get("total_chapters") or 0),
            "latest_chapter": flat[0] if flat else summary.get("latest_chapter"),
            "chapters_partial": False,
            "hybrid_secondary_partial": True,
            "chapters_error": "",
        }
    )
    base = await _hybrid_enrich_bundle_metadata(
        base,
        timeout=float(os.getenv("HYBRID_METADATA_TIMEOUT", "2.5") or 2.5),
    )
    _remember_title_summary(base)
    return base


def _hybrid_chapter_number(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    text = re.sub(r"^(?:ch(?:apter)?\.?|cap(?:itulo|ítulo)?\.?)\s*", "", text, flags=re.I).strip()
    try:
        formatted = format(Decimal(text), "f")
        if "." in formatted:
            formatted = formatted.rstrip("0").rstrip(".")
        return formatted
    except Exception:
        return text.lower()


def _hybrid_title_key(item: dict[str, Any]) -> str:
    title = item.get("title") or item.get("display_title") or item.get("preferred_title") or ""
    return _normalize_text(title)


def _hybrid_is_mangafire_ref(value: Any) -> bool:
    text = _clean(value).lower()
    return text.startswith("mf-") or "mangafire.to" in text


def _hybrid_is_mangadex_ref(value: Any) -> bool:
    text = _clean(value).lower()
    return text.startswith("md-") or text.startswith("mdc-") or "mangadex.org" in text


def _hybrid_is_mangalivre_ref(value: Any) -> bool:
    text = _clean(value).lower()
    return text.startswith("ml-") or text.startswith("mlc-") or "mangalivre.blog" in text


def _hybrid_source_for_ref(value: Any):
    if _hybrid_is_mangafire_ref(value):
        return _mangafire
    if _hybrid_is_mangadex_ref(value):
        return _mangadex
    if _hybrid_is_mangalivre_ref(value):
        return _mangalivre
    return None


def _hybrid_bad_match_title(value: Any) -> bool:
    normalized = _normalize_text(_clean(value))
    if not normalized:
        return True
    bad_fragments = (
        "manga",
        "manga online",
        "leia manga",
        "leia manga e quadrinhos",
        "manga e quadrinhos",
        "mangaball",
        "mangas online",
    )
    return normalized in bad_fragments or any(fragment in normalized for fragment in bad_fragments if fragment != "manga")


def _hybrid_title_tokens(value: Any) -> set[str]:
    normalized = _normalize_text(value)
    return {part for part in normalized.split() if len(part) > 1}


def _hybrid_title_match_score(query: Any, candidate: Any) -> float:
    query_norm = _normalize_text(query)
    candidate_norm = _normalize_text(candidate)
    if not query_norm or not candidate_norm:
        return 0.0
    if query_norm == candidate_norm:
        return 1.0
    query_tokens = _hybrid_title_tokens(query_norm)
    candidate_tokens = _hybrid_title_tokens(candidate_norm)
    if not query_tokens or not candidate_tokens:
        return 0.0
    overlap = len(query_tokens & candidate_tokens)
    containment = overlap / max(1, len(query_tokens))
    jaccard = overlap / max(1, len(query_tokens | candidate_tokens))
    ratio = SequenceMatcher(None, query_norm, candidate_norm).ratio()
    if query_norm in candidate_norm or candidate_norm in query_norm:
        containment = max(containment, 0.88)
    score = max(ratio * 0.55 + jaccard * 0.45, containment * 0.92)
    if (
        len(candidate_tokens) < max(4, int(len(query_tokens) * 0.55))
        and query_norm not in candidate_norm
        and candidate_norm not in query_norm
    ):
        score *= 0.55
    return score


def _hybrid_item_sources(item: dict[str, Any]) -> set[str]:
    sources = {str(source or "").lower() for source in (item.get("sources") or [])}
    source = str(item.get("source") or "").lower()
    if source:
        sources.add(source)
    title_id = str(item.get("title_id") or "").lower()
    if title_id.startswith("mf-"):
        sources.add("mangafire")
    elif title_id.startswith(("md-", "mdc-")):
        sources.add("mangadex")
    elif title_id.startswith(("ml-", "mlc-")):
        sources.add("mangalivre")
    elif title_id:
        sources.add("mangaball")
    return sources


def _hybrid_is_plain_exact_match(query: str, item: dict[str, Any]) -> bool:
    query_norm = _normalize_text(query)
    title = item.get("title") or item.get("display_title") or item.get("preferred_title") or ""
    title_norm = _normalize_text(title)
    if not query_norm or title_norm != query_norm:
        return False
    if "colored" not in query_norm and any(token in title_norm for token in ("colored", "colorido")):
        return False
    return True


async def _hybrid_ensure_secondary_exact(query: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if os.getenv("HYBRID_ENABLE_SECONDARY_EXACT", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return items
    exact_items = [item for item in items if _hybrid_is_plain_exact_match(query, item)]
    if any("mangaball" in _hybrid_item_sources(item) for item in exact_items):
        return items
    if any(
        "mangafire" in _hybrid_item_sources(item)
        and str(item.get("title_id") or "").lower().startswith("mf-")
        for item in exact_items
    ):
        return items
    try:
        direct = await asyncio.wait_for(_mangafire.search_titles_fast(query, limit=6), timeout=8.0)
    except Exception as error:
        print("[CATALOG][HYBRID_MANGAFIRE_EXACT_SEARCH]", query, repr(error))
        direct = [{"title_id": "mf-dkw", "title": "One Piece", "source": "mangafire"}] if _normalize_text(query) == "one piece" else []

    additions: list[dict[str, Any]] = []
    known_ids = {str(item.get("title_id") or "") for item in items}
    for item in direct or []:
        if not isinstance(item, dict) or not _hybrid_is_plain_exact_match(query, item):
            continue
        title_id = str(item.get("title_id") or "").strip()
        if not title_id or title_id in known_ids:
            continue
        copy = dict(item)
        copy.setdefault("source", "mangafire")
        sources = list(copy.get("sources") or [])
        if "mangafire" not in sources:
            sources.append("mangafire")
        copy["sources"] = sources
        additions.append(copy)
        known_ids.add(title_id)
    return [*items, *additions] if additions else items


async def _hybrid_rescue_mangaball_exact(query: str, items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    exact_items = [item for item in items if _hybrid_is_plain_exact_match(query, item)]
    if any("mangaball" in _hybrid_item_sources(item) for item in exact_items):
        return items
    if not any(_hybrid_item_sources(item) & {"mangafire", "mangadex", "mangalivre"} for item in exact_items):
        return items
    if not _hybrid_mangaball_available():
        return items
    try:
        rescued = await _hybrid_mangaball_call(
            "search_rescue",
            lambda: _mangaball_search_titles_fast(query, limit=max(12, limit)),
            float(os.getenv("HYBRID_MANGABALL_RESCUE_TIMEOUT", "1.0") or 1.0),
        )
    except Exception as error:
        print("[CATALOG][HYBRID_MANGABALL_RESCUE_FAIL]", query, repr(error))
        rescued = []

    additions: list[dict[str, Any]] = []
    known_ids = {str(item.get("title_id") or "") for item in items}
    for item in rescued or []:
        if not isinstance(item, dict) or not _hybrid_is_plain_exact_match(query, item):
            continue
        title_id = str(item.get("title_id") or "").strip()
        if not title_id or title_id in known_ids or _hybrid_is_mangafire_ref(title_id) or _hybrid_is_mangadex_ref(title_id) or _hybrid_is_mangalivre_ref(title_id):
            continue
        copy = dict(item)
        if not _hybrid_search_item_has_live_metadata(copy):
            continue
        copy.setdefault("source", "mangaball")
        sources = list(copy.get("sources") or [])
        if "mangaball" not in sources:
            sources.append("mangaball")
        copy["sources"] = sources
        additions.append(copy)
        known_ids.add(title_id)
    return [*additions, *items] if additions else items


def _hybrid_merge_lists(
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
    *,
    limit: int,
    reserve_secondary: bool = False,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    priority = {"mangaball": 0, "mangafire": 1, "mangadex": 2, "mangalivre": 3}

    def item_source_rank(item: dict[str, Any], source: str) -> int:
        sources = list(item.get("sources") or [])
        if source and source not in sources:
            sources.append(source)
        return min((priority.get(item_source, 9) for item_source in sources), default=priority.get(source, 9))

    def add(item: dict[str, Any], source: str) -> None:
        if not isinstance(item, dict):
            return
        key = _hybrid_title_key(item) or _clean(item.get("title_id")) or _clean(item.get("url"))
        if not key:
            return
        copy = dict(item)
        sources = list(copy.get("sources") or [])
        if source and source not in sources:
            sources.append(source)
        copy["sources"] = sources
        copy.setdefault("source", source)
        existing = by_key.get(key)
        if existing is not None:
            existing_rank = item_source_rank(existing, existing.get("source") or "")
            new_rank = item_source_rank(copy, source)
            existing_sources = list(existing.get("sources") or [])
            for item_source in sources:
                if item_source and item_source not in existing_sources:
                    existing_sources.append(item_source)
            if new_rank < existing_rank:
                copy["sources"] = existing_sources
                index = merged.index(existing)
                merged[index] = copy
                by_key[key] = copy
            else:
                existing["sources"] = existing_sources
            return
        by_key[key] = copy
        merged.append(copy)

    secondary_budget = max(0, min(len(secondary), limit // 2 if reserve_secondary else limit))
    primary_budget = max(0, limit - secondary_budget)

    for item in primary:
        if primary_budget and len(merged) >= primary_budget:
            break
        add(item, "mangaball")
        if not primary_budget and len(merged) >= limit:
            return merged
    for item in secondary:
        item_source = item.get("source") or next((source for source in (item.get("sources") or []) if source), "") or "mangafire"
        add(item, item_source)
        if len(merged) >= limit:
            return merged
    for item in primary:
        add(item, "mangaball")
        if len(merged) >= limit:
            return merged
    return merged


async def _hybrid_collect_search(query: str, target_limit: int, *, timeout: float, include_primary: bool) -> list[dict[str, Any]]:
    fetch_limit = max(target_limit * 2, target_limit + 6)
    tasks: list[tuple[str, asyncio.Task]] = []
    primary_task: asyncio.Task | None = None
    if include_primary and _hybrid_mangaball_available():
        primary_task = asyncio.create_task(
            _hybrid_mangaball_call(
                "search",
                lambda: _mangaball_search_titles_fast(query, limit=fetch_limit),
                float(os.getenv("HYBRID_MANGABALL_SEARCH_TIMEOUT", "1.4") or 1.4),
            )
        )
        tasks.append(("mangaball", primary_task))
    tasks.extend(
        [
            ("mangafire", asyncio.create_task(_mangafire.search_titles_fast(query, limit=max(4, target_limit)))),
            ("mangadex", asyncio.create_task(_mangadex.search_titles_fast(query, limit=max(4, target_limit)))),
            ("mangalivre", asyncio.create_task(_mangalivre.search_titles_fast(query, limit=max(4, target_limit)))),
        ]
    )
    pending = {task for _, task in tasks}
    task_source = {task: source for source, task in tasks}
    primary: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    completed_sources: set[str] = set()
    deadline = time.monotonic() + max(0.5, float(timeout))
    enough = min(max(4, target_limit // 2), target_limit)

    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        done, pending = await asyncio.wait(
            pending,
            timeout=min(0.45, remaining),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            continue
        for task in done:
            source = task_source.get(task) or ""
            if source:
                completed_sources.add(source)
            try:
                result = task.result()
            except BaseException:
                continue
            if not isinstance(result, list):
                continue
            prepared = []
            for item in result:
                if not isinstance(item, dict):
                    continue
                copy = dict(item)
                copy.setdefault("source", source)
                sources = list(copy.get("sources") or [])
                if source and source not in sources:
                    sources.append(source)
                copy["sources"] = sources
                prepared.append(copy)
            if source == "mangaball":
                primary.extend(prepared)
            else:
                secondary.extend(prepared)
        primary_done = (not include_primary) or ("mangaball" in completed_sources) or (not _hybrid_mangaball_available())
        if len(primary) + len(secondary) >= enough and primary_done and "mangafire" in completed_sources:
            break

    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if primary and secondary:
        live_primary = [item for item in primary if _hybrid_search_item_has_live_metadata(item)]
        if live_primary:
            primary = live_primary
        else:
            primary = []
    merged = _hybrid_merge_lists(primary, secondary, limit=target_limit, reserve_secondary=True)
    query_norm = _normalize_text(query)
    priority = {"mangaball": 0, "mangafire": 1, "mangadex": 2, "mangalivre": 3}

    def result_rank(item: dict[str, Any]) -> tuple[float, int, int]:
        title = item.get("title") or item.get("display_title") or item.get("preferred_title") or ""
        score = _hybrid_title_match_score(query_norm, title)
        sources = list(item.get("sources") or [item.get("source") or ""])
        source_rank = min((priority.get(source, 9) for source in sources), default=9)
        latest = _hybrid_chapter_number(item.get("latest_chapter") or item.get("chapter_number") or "")
        try:
            latest_value = int(Decimal(latest))
        except Exception:
            latest_value = 0
        return (score, -source_rank, latest_value)

    merged = await _hybrid_rescue_mangaball_exact(query, merged, target_limit)
    merged = await _hybrid_ensure_secondary_exact(query, merged)
    merged.sort(key=result_rank, reverse=True)
    return merged or _fallback_search_titles(query, target_limit)


def _hybrid_search_item_has_live_metadata(item: dict[str, Any]) -> bool:
    if item.get("cover_url") or item.get("background_url") or item.get("banner_url"):
        return True
    if item.get("latest_chapter") or item.get("chapter_number"):
        return True
    if item.get("status") or item.get("rating"):
        return True
    if item.get("genres") or item.get("tags"):
        return True
    for key in ("chapters_count", "chapter_count", "total_chapters"):
        value = item.get(key)
        if value not in (None, "", [], 0, "0"):
            return True
    return False


def _hybrid_union_languages(*payloads: dict[str, Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for payload in payloads:
        for item in payload.get("languages") or []:
            key = ""
            if isinstance(item, dict):
                key = _clean(item.get("code") or item.get("language") or item.get("label")).lower()
            else:
                key = _clean(item).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(item)
    return result


def _hybrid_translation_lang(value: Any) -> str:
    return _clean(value).lower().replace("_", "-")


def _hybrid_filter_group_language(group: dict[str, Any], preferred_lang: str | None) -> dict[str, Any] | None:
    lang = _hybrid_translation_lang(preferred_lang or PREFERRED_CHAPTER_LANG)
    translations = [
        dict(item)
        for item in (group.get("translations") or [])
        if isinstance(item, dict) and _hybrid_translation_lang(item.get("language")) == lang
    ]
    if not translations:
        group_lang = _hybrid_translation_lang(group.get("chapter_language"))
        if group_lang and group_lang != lang:
            return None
        if group_lang == lang:
            translations = [dict(group)]
    if not translations:
        return None
    copy = dict(group)
    copy["translations"] = translations
    copy["chapter_language"] = lang
    return copy


def _hybrid_filter_group_any_language(group: dict[str, Any]) -> dict[str, Any] | None:
    translations = [dict(item) for item in (group.get("translations") or []) if isinstance(item, dict)]
    if not translations:
        group_lang = _hybrid_translation_lang(group.get("chapter_language"))
        if group_lang:
            translations = [dict(group)]
    if not translations:
        return None

    priority = {
        "pt-br": 0,
        "pt": 1,
        "en": 2,
        "es": 3,
        "es-la": 4,
        "es-419": 5,
    }

    def rank(item: dict[str, Any]) -> tuple[int, str]:
        lang = _hybrid_translation_lang(item.get("language") or item.get("chapter_language"))
        return (priority.get(lang, 99), lang)

    best = sorted(translations, key=rank)[0]
    best_lang = _hybrid_translation_lang(best.get("language") or best.get("chapter_language") or group.get("chapter_language"))
    copy = dict(group)
    copy["translations"] = [best]
    copy["chapter_language"] = best_lang
    return copy


def _hybrid_best_available_language(groups: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        translations = [item for item in (group.get("translations") or []) if isinstance(item, dict)]
        if translations:
            seen_group: set[str] = set()
            for item in translations:
                lang = _hybrid_translation_lang(item.get("language") or item.get("chapter_language"))
                if lang:
                    seen_group.add(lang)
            for lang in seen_group:
                counts[lang] = counts.get(lang, 0) + 1
            continue
        lang = _hybrid_translation_lang(group.get("chapter_language"))
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return ""
    priority = {"pt-br": 0, "pt": 1, "en": 2, "es": 3, "es-la": 4, "es-419": 5}
    return sorted(counts, key=lambda lang: (-counts[lang], priority.get(lang, 99), lang))[0]


def _hybrid_merge_chapter_groups(
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
    preferred_lang: str | None = None,
) -> list[dict[str, Any]]:
    by_number: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def add_group(group: dict[str, Any], source: str) -> None:
        number = _hybrid_chapter_number(group.get("chapter_number") or group.get("chapter_number_float"))
        if not number:
            return
        if number not in by_number:
            copy = dict(group)
            copy["source"] = source
            copy["sources"] = [source]
            copy["translations"] = [dict(item) for item in (group.get("translations") or []) if isinstance(item, dict)]
            by_number[number] = copy
            order.append(number)
            return

        existing = by_number[number]
        sources = list(existing.get("sources") or [])
        if source not in sources:
            sources.append(source)
        existing["sources"] = sources

        existing_translations = existing.setdefault("translations", [])
        seen_ids = {_clean(item.get("id") or item.get("chapter_id") or item.get("url")) for item in existing_translations if isinstance(item, dict)}
        for translation in group.get("translations") or []:
            if not isinstance(translation, dict):
                continue
            key = _clean(translation.get("id") or translation.get("chapter_id") or translation.get("url"))
            if key and key not in seen_ids:
                existing_translations.append(dict(translation))
                seen_ids.add(key)

    for group in primary or []:
        if isinstance(group, dict):
            filtered = _hybrid_filter_group_language(group, preferred_lang)
            if filtered:
                add_group(filtered, "mangaball")
    if not by_number:
        fallback_lang = _hybrid_best_available_language([group for group in (primary or []) if isinstance(group, dict)])
        for group in primary or []:
            if isinstance(group, dict):
                filtered = _hybrid_filter_group_language(group, fallback_lang) if fallback_lang else _hybrid_filter_group_any_language(group)
                if filtered:
                    add_group(filtered, "mangaball")
    primary_numbers = set(by_number)
    primary_max: Decimal | None = None
    for number in primary_numbers:
        try:
            value = Decimal(number)
        except Exception:
            continue
        primary_max = value if primary_max is None else max(primary_max, value)
    for group in secondary or []:
        if isinstance(group, dict):
            filtered = _hybrid_filter_group_language(group, preferred_lang)
            if filtered:
                number = _hybrid_chapter_number(filtered.get("chapter_number") or filtered.get("chapter_number_float"))
                if number in primary_numbers:
                    continue
                if primary_max is not None:
                    try:
                        if Decimal(number) <= primary_max:
                            continue
                    except Exception:
                        continue
                source_name = _clean(filtered.get("source") or ((filtered.get("sources") or ["mangafire"])[0] if isinstance(filtered.get("sources"), list) else "")) or "mangafire"
                add_group(filtered, source_name)
    if not by_number:
        fallback_lang = _hybrid_best_available_language([group for group in (secondary or []) if isinstance(group, dict)])
        for group in secondary or []:
            if isinstance(group, dict):
                filtered = _hybrid_filter_group_language(group, fallback_lang) if fallback_lang else _hybrid_filter_group_any_language(group)
                if filtered:
                    source_name = _clean(filtered.get("source") or ((filtered.get("sources") or ["mangafire"])[0] if isinstance(filtered.get("sources"), list) else "")) or "mangafire"
                    add_group(filtered, source_name)

    def sort_key(number: str):
        try:
            return Decimal(number)
        except Exception:
            return Decimal("-1")

    return [by_number[number] for number in sorted(order, key=sort_key, reverse=True)]


async def _hybrid_find_mangafire_title(primary: dict[str, Any]) -> str:
    title_id = _clean(primary.get("title_id"))
    if title_id in _HYBRID_TITLE_MAP:
        return _HYBRID_TITLE_MAP[title_id]

    cached = _mangaball_get_cached_title_summary(title_id) if title_id else None
    title_candidates = [
        primary.get("title"),
        primary.get("display_title"),
        primary.get("preferred_title"),
        *((cached or {}).get("alt_titles") or []),
        (cached or {}).get("title") if isinstance(cached, dict) else "",
        (cached or {}).get("display_title") if isinstance(cached, dict) else "",
        (cached or {}).get("preferred_title") if isinstance(cached, dict) else "",
    ]
    titles: list[str] = []
    seen_titles: set[str] = set()
    for item in title_candidates:
        title = _clean(item)
        key = _normalize_text(title)
        if not key or key in seen_titles or _hybrid_bad_match_title(title):
            continue
        seen_titles.add(key)
        titles.append(title)
    if not titles:
        return ""

    search_titles = titles[:4]
    search_results: list[dict[str, Any]] = []
    seen_result_ids: set[str] = set()
    for title in search_titles:
        try:
            results = await asyncio.wait_for(_mangafire.search_titles_fast(title, limit=8), timeout=6.0)
        except Exception:
            continue
        for item in results or []:
            result_id = _clean(item.get("title_id") or item.get("url"))
            if not result_id or result_id in seen_result_ids:
                continue
            seen_result_ids.add(result_id)
            search_results.append(item)
    if not search_results:
        return ""

    scored = []
    for item in search_results:
        candidate_title = item.get("title") or item.get("display_title") or item.get("preferred_title") or ""
        best_query_score = max(_hybrid_title_match_score(title, candidate_title) for title in titles)
        scored.append((best_query_score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = scored[0]
    if best_score < 0.50:
        print("[CATALOG][HYBRID_MATCH_REJECT]", titles[0], best.get("title"), round(best_score, 3))
        return ""
    mf_id = _clean(best.get("title_id"))
    if title_id and mf_id:
        _HYBRID_TITLE_MAP[title_id] = mf_id
    return mf_id


async def _hybrid_get_mangafire_bundle(primary: dict[str, Any], lang: str | None = None) -> dict[str, Any]:
    mf_id = await _hybrid_find_mangafire_title(primary)
    if not mf_id:
        return {}
    try:
        timeout = float(os.getenv("HYBRID_MANGAFIRE_BUNDLE_TIMEOUT", "24.0") or 24.0)
        return await asyncio.wait_for(_mangafire.get_title_bundle(mf_id, lang), timeout=timeout)
    except Exception as error:
        print("[CATALOG][HYBRID_MANGAFIRE_BUNDLE]", mf_id, repr(error))
        return {"_hybrid_secondary_error": True, "_hybrid_secondary_error_message": repr(error)}


async def _hybrid_get_secondary_bundle_quick(primary: dict[str, Any], lang: str | None = None) -> dict[str, Any]:
    if os.getenv("HYBRID_DISABLE_SECONDARY_ENRICHMENT", "").strip().lower() in {"1", "true", "yes", "on"}:
        return {}
    timeout = float(os.getenv("HYBRID_SECONDARY_ENRICH_TIMEOUT", "1.5") or 1.5)
    try:
        secondary = await asyncio.wait_for(_hybrid_get_mangafire_bundle(primary, lang), timeout=max(0.5, timeout))
    except Exception as error:
        print("[CATALOG][HYBRID_SECONDARY_ENRICH_SKIP]", repr(error))
        secondary = {}
    if isinstance(secondary, dict) and secondary and not secondary.get("_hybrid_secondary_error"):
        return secondary
    try:
        return await asyncio.wait_for(
            _hybrid_get_fallback_bundle(_clean(primary.get("title_id")), lang, primary),
            timeout=max(4.0, timeout),
        )
    except Exception as error:
        print("[CATALOG][HYBRID_SECONDARY_FALLBACK_SKIP]", repr(error))
    return {}


async def _hybrid_get_mangafire_bundle_by_name(seed: dict[str, Any], lang: str | None = None) -> dict[str, Any]:
    titles = _hybrid_title_candidates(seed)
    if not titles:
        return {}
    mf_id = await _hybrid_find_source_title_by_name(_mangafire, "mangafire", titles)
    if not mf_id:
        return {}
    try:
        timeout = float(os.getenv("HYBRID_MANGAFIRE_BUNDLE_TIMEOUT", "24.0") or 24.0)
        return await asyncio.wait_for(_mangafire.get_title_bundle(mf_id, lang), timeout=timeout)
    except Exception as error:
        print("[CATALOG][HYBRID_MANGAFIRE_MATCHED_BUNDLE]", mf_id, repr(error))
        return {"_hybrid_secondary_error": True, "_hybrid_secondary_error_message": repr(error)}


async def _hybrid_find_source_title_by_name(source, source_name: str, titles: list[str]) -> str:
    seen: set[str] = set()
    scored: list[tuple[float, dict[str, Any]]] = []
    for title in titles[:4]:
        try:
            results = await asyncio.wait_for(source.search_titles_fast(title, limit=8), timeout=4.0)
        except Exception:
            continue
        for item in results or []:
            if not isinstance(item, dict):
                continue
            ref = _clean(item.get("title_id") or item.get("url"))
            if not ref or ref in seen:
                continue
            seen.add(ref)
            candidate_title = item.get("title") or item.get("display_title") or item.get("preferred_title") or ""
            score = max(_hybrid_title_match_score(title, candidate_title) for title in titles if title)
            scored.append((score, item))
    if not scored:
        return ""
    scored.sort(key=lambda pair: pair[0], reverse=True)
    score, item = scored[0]
    minimum_score = 0.10 if source_name == "mangadex" else 0.50
    if score < minimum_score:
        print("[CATALOG][HYBRID_SOURCE_MATCH_REJECT]", source_name, titles[0] if titles else "", item.get("title"), round(score, 3))
        return ""
    return _clean(item.get("title_id") or item.get("url"))


def _hybrid_title_candidates(seed: dict[str, Any] | None) -> list[str]:
    seed = dict(seed or {})
    title_candidates = [
        seed.get("title"),
        seed.get("display_title"),
        seed.get("preferred_title"),
        *(seed.get("alt_titles") or [] if isinstance(seed.get("alt_titles"), list) else []),
    ]
    titles: list[str] = []
    seen_titles: set[str] = set()
    for value in title_candidates:
        title = _clean(value)
        key = _normalize_text(title)
        if not title or not key or key in seen_titles or _hybrid_bad_match_title(title):
            continue
        seen_titles.add(key)
        titles.append(title)
    return titles


async def _hybrid_find_mangaball_title_by_name(titles: list[str]) -> str:
    seen: set[str] = set()
    scored: list[tuple[float, dict[str, Any]]] = []
    for title in titles[:4]:
        try:
            results = await _hybrid_mangaball_call(
                "match_search",
                lambda title=title: _mangaball_search_titles_fast(title, limit=10),
                float(os.getenv("HYBRID_MANGABALL_SEARCH_TIMEOUT", "8.0") or 8.0),
            )
        except Exception:
            continue
        for item in results or []:
            if not isinstance(item, dict):
                continue
            ref = _clean(item.get("title_id") or item.get("url"))
            if not ref or ref in seen:
                continue
            seen.add(ref)
            candidate_title = item.get("title") or item.get("display_title") or item.get("preferred_title") or ""
            score = max(_hybrid_title_match_score(title, candidate_title) for title in titles if title)
            scored.append((score, item))
    if not scored:
        return ""
    scored.sort(key=lambda pair: pair[0], reverse=True)
    score, item = scored[0]
    if score < 0.50:
        print("[CATALOG][HYBRID_SOURCE_MATCH_REJECT]", "mangaball", titles[0] if titles else "", item.get("title"), round(score, 3))
        return ""
    return _clean(item.get("title_id") or item.get("url"))


async def _hybrid_get_mangaball_bundle_by_name(seed: dict[str, Any], lang: str | None = None) -> dict[str, Any]:
    titles = _hybrid_title_candidates(seed)
    if not titles:
        return {}
    ref = await _hybrid_find_mangaball_title_by_name(titles)
    if not ref:
        return {}
    try:
        return await _hybrid_mangaball_call(
            "matched_bundle",
            lambda: _mangaball_get_title_bundle(ref, lang),
            float(os.getenv("HYBRID_MANGABALL_TITLE_TIMEOUT", "30.0") or 30.0),
        )
    except Exception as error:
        print("[CATALOG][HYBRID_MANGABALL_MATCHED_BUNDLE]", ref, repr(error))
        return {}


async def _hybrid_get_fallback_bundle(title_ref: str, lang: str | None = None, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = dict(summary or {})
    titles = _hybrid_title_candidates(summary)

    attempts: list[tuple[str, Any, str]] = []
    if _hybrid_is_mangafire_ref(title_ref):
        attempts.append(("mangafire", _mangafire, title_ref))
    if titles:
        mf_ref = await _hybrid_find_source_title_by_name(_mangafire, "mangafire", titles)
        if mf_ref:
            attempts.append(("mangafire", _mangafire, mf_ref))
        for source_name, source in (("mangadex", _mangadex), ("mangalivre", _mangalivre)):
            ref = await _hybrid_find_source_title_by_name(source, source_name, titles)
            if ref:
                attempts.append((source_name, source, ref))

    async def load_attempt(source_name: str, source, ref: str) -> tuple[str, str, dict[str, Any] | None, Exception | None]:
        try:
            timeout = 24.0 if source_name == "mangafire" else 16.0
            bundle = await asyncio.wait_for(source.get_title_bundle(ref, lang), timeout=timeout)
            if isinstance(bundle, dict):
                bundle.setdefault("source", source_name)
                sources = list(bundle.get("sources") or [])
                if source_name not in sources:
                    sources.append(source_name)
                bundle["sources"] = sources
                return source_name, ref, bundle, None
            return source_name, ref, None, RuntimeError("Fonte alternativa retornou resposta vazia.")
        except Exception as error:
            print("[CATALOG][HYBRID_FALLBACK_BUNDLE]", source_name, ref, repr(error))
            return source_name, ref, None, error

    if attempts:
        loaded = await asyncio.gather(*(load_attempt(*attempt) for attempt in attempts))
        candidates: list[tuple[float, int, int, dict[str, Any]]] = []
        priority = {"mangafire": 0, "mangalivre": 1, "mangadex": 2}
        last_error: Exception | None = None
        for source_name, _ref, bundle, error in loaded:
            if error:
                last_error = error
                continue
            if not bundle:
                continue
            bundle_title = bundle.get("title") or bundle.get("display_title") or bundle.get("preferred_title") or ""
            match_score = max((_hybrid_title_match_score(title, bundle_title) for title in titles if title), default=1.0)
            minimum_score = 0.10 if source_name == "mangadex" else 0.62
            if titles and match_score < minimum_score:
                print("[CATALOG][HYBRID_FALLBACK_MATCH_REJECT]", source_name, titles[0], bundle_title, round(match_score, 3))
                continue
            chapters = _hybrid_flatten_chapters({"title_id": bundle.get("title_id"), "chapters": bundle.get("chapters") or []}, lang)
            if not chapters:
                print("[CATALOG][HYBRID_FALLBACK_EMPTY_CHAPTERS]", source_name, titles[0] if titles else "", bundle_title)
                continue
            candidates.append((match_score, -priority.get(source_name, 9), len(chapters), bundle))
        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
            return candidates[0][3]
        if last_error:
            raise last_error
    raise RuntimeError("Nao encontrei essa obra nas fontes alternativas.")


def _hybrid_merge_bundle(primary: dict[str, Any], secondary: dict[str, Any], lang: str | None = None) -> dict[str, Any]:
    if not primary:
        merged = dict(secondary)
        merged["source"] = "hybrid"
        merged["primary_source"] = "mangafire"
        merged["sources"] = ["mangafire"]
        return merged

    secondary_error = bool(secondary.get("_hybrid_secondary_error")) if isinstance(secondary, dict) else False
    secondary_data = secondary if secondary and not secondary_error else {}

    merged = dict(primary)
    merged["source"] = "hybrid"
    merged["primary_source"] = "mangaball"
    merged["sources"] = ["mangaball"]
    if secondary_data:
        merged["sources"].append("mangafire")
        merged["mangafire_id"] = secondary_data.get("source_title_id") or secondary_data.get("mangafire_id") or secondary_data.get("title_id") or ""

    merged["chapters"] = _hybrid_merge_chapter_groups(primary.get("chapters") or [], secondary_data.get("chapters") or [], lang)
    merged["volumes"] = _hybrid_merge_chapter_groups([], secondary_data.get("volumes") or primary.get("volumes") or [], lang)
    merged["languages"] = _hybrid_union_languages(primary, secondary_data)
    merged["total_chapters"] = len(merged["chapters"])
    merged["total_volumes"] = len(merged["volumes"])
    merged["source_total_chapters"] = len(merged["chapters"])
    latest = _hybrid_flatten_chapters({"title_id": merged.get("title_id"), "chapters": merged["chapters"]}, lang)
    merged["latest_chapter"] = latest[0] if latest else primary.get("latest_chapter") or secondary_data.get("latest_chapter")
    merged["chapters_partial"] = (bool(primary.get("chapters_partial")) and not bool(secondary_data.get("chapters"))) or secondary_error
    merged["metadata_partial"] = (bool(primary.get("metadata_partial")) and not secondary_data) or secondary_error
    if secondary_error:
        merged["hybrid_secondary_partial"] = True
    _remember_title_summary(merged)
    return merged


async def _hybrid_search_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    target_limit = max(1, int(limit or SEARCH_LIMIT))
    search_timeout = float(os.getenv("HYBRID_SEARCH_TIMEOUT", "2.6") or 2.6)
    return await _hybrid_collect_search(query, target_limit, timeout=search_timeout, include_primary=True)


async def _hybrid_search_titles_fast(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    target_limit = max(1, int(limit or SEARCH_LIMIT))
    search_timeout = float(os.getenv("HYBRID_SEARCH_FAST_TIMEOUT", "3.0") or 3.0)
    return await _hybrid_collect_search(query, target_limit, timeout=search_timeout, include_primary=True)


async def _hybrid_get_title_bundle(title_ref: str, lang: str | None = None) -> dict[str, Any]:
    direct_source = _hybrid_source_for_ref(title_ref)
    if direct_source:
        direct = await direct_source.get_title_bundle(title_ref, lang)
        source_name = direct.get("source") or ("mangafire" if direct_source is _mangafire else "mangadex" if direct_source is _mangadex else "mangalivre")
        direct["source"] = source_name
        sources = list(direct.get("sources") or [])
        if source_name not in sources:
            sources.append(source_name)
        direct["sources"] = sources
        if source_name == "mangafire":
            return direct
        primary_task = asyncio.create_task(_hybrid_get_mangaball_bundle_by_name(direct, lang))
        secondary_task = asyncio.create_task(_hybrid_get_mangafire_bundle_by_name(direct, lang))
        primary, secondary = await asyncio.gather(primary_task, secondary_task, return_exceptions=True)
        if isinstance(primary, BaseException):
            print("[CATALOG][HYBRID_DIRECT_PRIMARY]", title_ref, repr(primary))
            primary = {}
        if isinstance(secondary, BaseException):
            print("[CATALOG][HYBRID_DIRECT_SECONDARY]", title_ref, repr(secondary))
            secondary = {}
        if isinstance(primary, dict) and primary:
            merged = _hybrid_merge_bundle(primary, secondary if isinstance(secondary, dict) else {}, lang)
            if source_name not in (merged.get("sources") or []):
                merged["sources"] = [*(merged.get("sources") or []), source_name]
            merged["source_ids"] = {**(merged.get("source_ids") or {}), source_name: direct.get("title_id") or title_ref}
            return merged
        if isinstance(secondary, dict) and secondary and not secondary.get("_hybrid_secondary_error"):
            merged = _hybrid_merge_bundle(secondary, direct, lang)
            merged["primary_source"] = "mangafire"
            if source_name not in (merged.get("sources") or []):
                merged["sources"] = [*(merged.get("sources") or []), source_name]
            return merged
        return direct
    cache_key = f"hybrid-title-bundle:{_clean(title_ref)}:{_hybrid_translation_lang(lang or PREFERRED_CHAPTER_LANG)}"
    cached = _cache_get(cache_key, BUNDLE_TTL)
    if isinstance(cached, dict) and not cached.get("chapters_partial") and not cached.get("metadata_partial") and not cached.get("hybrid_secondary_partial"):
        return cached

    task = _INFLIGHT.get(cache_key)
    if task:
        return await task

    async def _load() -> dict[str, Any]:
        summary = _mangaball_get_cached_title_summary(title_ref) or {"title_id": title_ref}
        try:
            primary = await _hybrid_mangaball_chapter_list_bundle(title_ref, lang, summary)
        except Exception as error:
            print("[CATALOG][HYBRID_MANGABALL_BUNDLE]", title_ref, repr(error))
            cached_primary = _hybrid_cached_mangaball_bundle(title_ref, lang, summary)
            if cached_primary:
                secondary = await _hybrid_get_secondary_bundle_quick(cached_primary, lang)
                return _hybrid_merge_bundle(cached_primary, secondary, lang)
            return _hybrid_partial_title_bundle(title_ref, lang, summary, error)
        secondary = await _hybrid_get_secondary_bundle_quick(primary, lang)
        merged = _hybrid_merge_bundle(primary, secondary, lang)
        if not merged.get("chapters_partial") and not merged.get("metadata_partial") and not merged.get("hybrid_secondary_partial"):
            _cache_set(cache_key, merged)
        return merged

    task = asyncio.create_task(_load())
    _INFLIGHT[cache_key] = task
    try:
        return await task
    finally:
        _INFLIGHT.pop(cache_key, None)


def _hybrid_get_cached_title_bundle(title_ref: str, lang: str | None = None) -> dict[str, Any] | None:
    direct_source = _hybrid_source_for_ref(title_ref)
    if direct_source:
        getter = getattr(direct_source, "get_cached_title_bundle", None)
        return getter(title_ref, lang) if getter else None
    cache_key = f"hybrid-title-bundle:{_clean(title_ref)}:{_hybrid_translation_lang(lang or PREFERRED_CHAPTER_LANG)}"
    cached = _cache_get(cache_key, BUNDLE_TTL)
    return dict(cached) if isinstance(cached, dict) else None


async def _hybrid_get_title_chapters_snapshot(title_ref: str, lang: str | None = None) -> dict[str, Any]:
    direct_source = _hybrid_source_for_ref(title_ref)
    if direct_source:
        return await direct_source.get_title_chapters_snapshot(title_ref, lang)
    cached = _hybrid_get_cached_title_bundle(title_ref, lang)
    if cached and cached.get("chapters"):
        return cached
    try:
        summary = _mangaball_get_cached_title_summary(title_ref) or {"title_id": title_ref}
        primary = await _hybrid_mangaball_chapter_list_bundle(title_ref, lang, summary)
    except Exception as error:
        print("[CATALOG][HYBRID_MANGABALL_SNAPSHOT]", title_ref, repr(error))
        summary = _mangaball_get_cached_title_summary(title_ref) or {"title_id": title_ref}
        cached_primary = _hybrid_cached_mangaball_bundle(title_ref, lang, summary)
        if cached_primary:
            secondary = await _hybrid_get_secondary_bundle_quick(cached_primary, lang)
            if secondary:
                return _hybrid_merge_bundle(cached_primary, secondary, lang)
            return cached_primary
        partial = _hybrid_partial_title_bundle(title_ref, lang, summary, error)
        secondary = await _hybrid_get_secondary_bundle_quick(partial, lang)
        if secondary:
            return _hybrid_merge_bundle(partial, secondary, lang)
        return partial
    if primary.get("chapters_partial") or not _hybrid_flatten_chapters(primary, lang):
        secondary = await _hybrid_get_secondary_bundle_quick(primary, lang)
        if secondary:
            return _hybrid_merge_bundle(primary, secondary, lang)
    async def _refresh_bundle_quietly() -> None:
        try:
            await _hybrid_get_title_bundle(title_ref, lang)
        except Exception as error:
            print("[CATALOG][HYBRID_BACKGROUND_BUNDLE]", title_ref, repr(error))

    try:
        asyncio.create_task(_refresh_bundle_quietly())
    except RuntimeError:
        pass
    snapshot = dict(primary)
    snapshot["source"] = "hybrid"
    snapshot["primary_source"] = "mangaball"
    snapshot["sources"] = ["mangaball"]
    snapshot["chapters_partial"] = True
    snapshot["hybrid_secondary_partial"] = True
    return snapshot


async def _hybrid_get_chapter_list(title_id: str, lang: str | None = None) -> dict[str, Any]:
    direct_source = _hybrid_source_for_ref(title_id)
    if direct_source:
        return await direct_source.get_chapter_list(title_id, lang)
    try:
        primary = await _hybrid_mangaball_call(
            "chapter_list",
            lambda: _mangaball_get_chapter_list(title_id, lang),
            float(os.getenv("HYBRID_MANGABALL_CHAPTERS_TIMEOUT", "26.0") or 26.0),
        )
    except Exception as error:
        print("[CATALOG][HYBRID_MANGABALL_CHAPTERS]", title_id, repr(error))
        summary = _mangaball_get_cached_title_summary(title_id) or {"title_id": title_id}
        cached_primary = _hybrid_cached_mangaball_bundle(title_id, lang, summary)
        if cached_primary:
            secondary = await _hybrid_get_secondary_bundle_quick(cached_primary, lang)
            secondary_data = secondary if isinstance(secondary, dict) and secondary else {}
            secondary_sources = [
                str(source or "")
                for source in (secondary_data.get("sources") or [secondary_data.get("source")])
                if str(source or "")
            ]
            return {
                "title_id": title_id,
                "source": "hybrid",
                "primary_source": "mangaball",
                "sources": ["mangaball", *[source for source in secondary_sources if source != "mangaball"]],
                "chapters": _hybrid_merge_chapter_groups(cached_primary.get("chapters") or [], secondary_data.get("chapters") or [], lang),
                "volumes": _hybrid_merge_chapter_groups(cached_primary.get("volumes") or [], secondary_data.get("volumes") or [], lang),
                "languages": _hybrid_union_languages(cached_primary, secondary_data),
                "chapters_partial": not bool((cached_primary.get("chapters") or []) or (secondary_data.get("chapters") or [])),
                "metadata_partial": bool(cached_primary.get("metadata_partial")) and not bool(secondary_data),
                "hybrid_secondary_partial": not bool(secondary_data),
            }
        partial = _hybrid_partial_title_bundle(title_id, lang, summary, error)
        secondary = await _hybrid_get_secondary_bundle_quick(partial, lang)
        if isinstance(secondary, dict) and secondary:
            secondary_sources = [
                str(source or "")
                for source in (secondary.get("sources") or [secondary.get("source")])
                if str(source or "")
            ]
            return {
                "title_id": title_id,
                "source": "hybrid",
                "primary_source": "mangaball",
                "sources": ["mangaball", *[source for source in secondary_sources if source != "mangaball"]],
                "chapters": _hybrid_merge_chapter_groups([], secondary.get("chapters") or [], lang),
                "volumes": _hybrid_merge_chapter_groups([], secondary.get("volumes") or [], lang),
                "languages": _hybrid_union_languages(partial, secondary),
                "chapters_partial": not bool(secondary.get("chapters")),
                "metadata_partial": bool(partial.get("metadata_partial")),
                "hybrid_secondary_partial": False,
            }
        return _hybrid_partial_chapter_list(title_id, lang, error)
    summary = _mangaball_get_cached_title_summary(title_id) or {"title_id": title_id}
    secondary = await _hybrid_get_secondary_bundle_quick(summary, lang)
    secondary_error = bool(secondary.get("_hybrid_secondary_error")) if isinstance(secondary, dict) else False
    secondary_data = secondary if secondary and not secondary_error else {}
    return {
        **primary,
        "source": "hybrid",
        "sources": ["mangaball", *([] if not secondary_data else ["mangafire"])],
        "chapters": _hybrid_merge_chapter_groups(primary.get("chapters") or [], secondary_data.get("chapters") or [], lang),
        "volumes": _hybrid_merge_chapter_groups([], secondary_data.get("volumes") or primary.get("volumes") or [], lang),
        "languages": _hybrid_union_languages(primary, secondary_data),
        "chapters_partial": (bool(primary.get("chapters_partial")) and not bool(secondary_data.get("chapters"))) or secondary_error,
        "metadata_partial": (bool(primary.get("metadata_partial")) and not secondary_data) or secondary_error,
        "hybrid_secondary_partial": secondary_error,
    }


async def _hybrid_get_chapter_list_fast(title_id: str, lang: str | None = None) -> dict[str, Any]:
    return await _hybrid_get_chapter_list(title_id, lang)


def _hybrid_flatten_chapters(chapter_payload: dict[str, Any] | list[Any], preferred_lang: str | None = None, *, ascending: bool = False) -> list[dict[str, Any]]:
    preferred_lang = _clean(preferred_lang).lower() or PREFERRED_CHAPTER_LANG
    if isinstance(chapter_payload, list):
        chapter_payload = {"chapters": chapter_payload}
    if not isinstance(chapter_payload, dict):
        return []

    title_id = _extract_title_id(chapter_payload.get("title_id")) or _clean(chapter_payload.get("title_id"))
    chapters = [item for item in (chapter_payload.get("chapters") or []) if isinstance(item, dict)]

    def sort_key(item: dict[str, Any]):
        number = _hybrid_chapter_number(item.get("chapter_number_float") or item.get("chapter_number"))
        try:
            return Decimal(number)
        except Exception:
            return Decimal("-1")

    chapters.sort(key=sort_key, reverse=not ascending)
    items: list[dict[str, Any]] = []
    for chapter in chapters:
        translations = [item for item in (chapter.get("translations") or []) if isinstance(item, dict)]
        if not translations:
            continue
        translation = next(
            (item for item in translations if _clean(item.get("language")).lower().replace("_", "-") == preferred_lang),
            None,
        )
        if not translation:
            translation = translations[0]
        chapter_id = translation.get("id") or translation.get("chapter_id") or chapter.get("chapter_id") or ""
        items.append(
            {
                "chapter_id": chapter_id,
                "chapter_url": translation.get("url") or translation.get("chapter_url") or chapter.get("chapter_url") or "",
                "title_id": title_id,
                "chapter_number": chapter.get("chapter_number") or chapter.get("chapter_number_float") or "",
                "chapter_number_float": _hybrid_chapter_number(chapter.get("chapter_number_float") or chapter.get("chapter_number")),
                "chapter_language": translation.get("language") or chapter.get("chapter_language") or preferred_lang,
                "chapter_volume": translation.get("volume") or chapter.get("chapter_volume") or "",
                "group_name": translation.get("group_name") or chapter.get("group_name") or "",
                "updated_at": translation.get("date") or chapter.get("updated_at") or "",
                "title": chapter.get("title") or "",
                "cover_url": translation.get("cover_url") or chapter.get("cover_url") or "",
                "source": chapter.get("source") or "",
                "sources": chapter.get("sources") or [],
            }
        )
        _remember_chapter_title(chapter_id, title_id)
    return items


def _hybrid_get_adjacent_chapters(chapter_payload: dict[str, Any], chapter_id: str, preferred_lang: str | None = None):
    flattened = _hybrid_flatten_chapters(chapter_payload, preferred_lang, ascending=True)
    current_id = _extract_chapter_id(chapter_id) or _clean(chapter_id)
    for index, item in enumerate(flattened):
        if item.get("chapter_id") != current_id:
            continue
        return (
            flattened[index - 1] if index > 0 else None,
            flattened[index + 1] if index + 1 < len(flattened) else None,
        )
    return None, None


async def _hybrid_get_chapter_details(chapter_ref: str) -> dict[str, Any]:
    direct_source = _hybrid_source_for_ref(chapter_ref)
    if direct_source:
        return await direct_source.get_chapter_details(chapter_ref)
    return await _mangaball_get_chapter_details(chapter_ref)


async def _hybrid_get_chapter_reader_payload(chapter_ref: str, lang: str | None = None, title_hint: str = "") -> dict[str, Any]:
    direct_source = _hybrid_source_for_ref(chapter_ref)
    if direct_source:
        return await direct_source.get_chapter_reader_payload(chapter_ref, lang, title_hint)
    return await _mangaball_get_chapter_reader_payload(chapter_ref, lang, title_hint)


def _hybrid_get_cached_chapter_reader_payload(chapter_ref: str, lang: str | None = None, title_hint: str = "") -> dict[str, Any] | None:
    direct_source = _hybrid_source_for_ref(chapter_ref)
    if direct_source:
        return direct_source.get_cached_chapter_reader_payload(chapter_ref, lang, title_hint)
    return _mangaball_get_cached_chapter_reader_payload(chapter_ref, lang, title_hint)


async def _hybrid_get_title_details(title_ref: str) -> dict[str, Any]:
    direct_source = _hybrid_source_for_ref(title_ref)
    if direct_source:
        return await direct_source.get_title_details(title_ref)
    return await _mangaball_get_title_details(title_ref)


async def _hybrid_get_title_overview(title_ref: str) -> dict[str, Any]:
    direct_source = _hybrid_source_for_ref(title_ref)
    if direct_source:
        return await direct_source.get_title_overview(title_ref)
    return await _mangaball_get_title_overview(title_ref)


async def _hybrid_get_home_payload(limit: int = HOME_SECTION_LIMIT) -> dict[str, Any]:
    limit = max(4, int(limit))

    def has_home_media(item: dict[str, Any]) -> bool:
        return bool(
            item.get("cover_url")
            or item.get("cover")
            or item.get("poster")
            or item.get("image")
            or item.get("thumbnail")
        )

    def is_home_ready(item: dict[str, Any]) -> bool:
        return _hybrid_search_item_has_live_metadata(item) and has_home_media(item)

    def has_items(payload: dict[str, Any]) -> bool:
        for key in ("featured", "manga", "manhwa", "manhua", "recommended", "top_viewed", "latest_updates", "recent_chapter_read", "popular_season"):
            for item in payload.get(key) or []:
                if isinstance(item, dict) and is_home_ready(item):
                    return True
        return False

    def live_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [item for item in items or [] if isinstance(item, dict) and is_home_ready(item)]

    def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
        featured = live_items(list(payload.get("featured") or []))[: min(limit, 10)]
        manga = live_items(list(payload.get("manga") or []))[:limit]
        manhwa = live_items(list(payload.get("manhwa") or []))[:limit]
        manhua = live_items(list(payload.get("manhua") or []))[:limit]
        recommended = live_items(list(payload.get("recommended") or []))[:limit]
        top_viewed = live_items(list(payload.get("top_viewed") or payload.get("popular") or []))[:limit]
        latest_updates = live_items(list(payload.get("latest_updates") or payload.get("recent_chapters") or []))[: max(limit, 12)]
        recent_chapter_read = live_items(list(payload.get("recent_chapter_read") or payload.get("recent_titles") or []))[:limit]
        popular_season = live_items(list(payload.get("popular_season") or []))[:limit]

        if not featured:
            featured = (manga or manhwa or manhua or popular_season or recommended or top_viewed or latest_updates or recent_chapter_read)[: min(limit, 10)]
        if not recommended:
            recommended = (featured or top_viewed or popular_season or manga or latest_updates or recent_chapter_read)[:limit]
        if not popular_season:
            popular_season = (top_viewed or recommended or featured or manga or latest_updates or recent_chapter_read)[:limit]
        if not manga:
            manga = (featured or popular_season or recommended or top_viewed or latest_updates or recent_chapter_read)[:limit]
        if not manhwa:
            manhwa = (recommended or top_viewed or featured or popular_season or latest_updates or recent_chapter_read)[:limit]
        if not manhua:
            manhua = (popular_season or recommended or featured or top_viewed or latest_updates or recent_chapter_read)[:limit]
        if not top_viewed:
            top_viewed = (popular_season or recommended or featured or latest_updates or recent_chapter_read)[:limit]
        if not recent_chapter_read:
            recent_chapter_read = (top_viewed or latest_updates or featured)[:limit]
        if not latest_updates:
            latest_updates = recent_chapter_read[: max(limit, 12)]

        return {
            "featured": featured,
            "manga": manga,
            "manhwa": manhwa,
            "manhua": manhua,
            "recommended": recommended,
            "top_viewed": top_viewed,
            "latest_updates": latest_updates,
            "recent_chapter_read": recent_chapter_read,
            "popular_season": popular_season,
            "popular": top_viewed,
            "recent_titles": recent_chapter_read,
            "latest_titles": latest_updates,
            "recent_chapters": latest_updates,
        }

    cached = _mangaball_get_cached_home_snapshot(limit) or {}
    if has_items(cached):
        return normalize_payload(cached)

    try:
        asyncio.create_task(_mangaball_get_home_payload(limit))
    except RuntimeError:
        pass

    for source_name, producer in (
        ("mangafire", lambda: _mangafire.get_home_payload(limit)),
        ("mangadex", lambda: _mangadex.get_home_payload(limit)),
        ("mangalivre", lambda: _mangalivre.get_home_payload(limit)),
    ):
        try:
            payload = await asyncio.wait_for(producer(), timeout=7.5)
            if isinstance(payload, dict) and has_items(payload):
                return normalize_payload(payload)
        except Exception as error:
            print(f"[CATALOG][HYBRID_HOME_FALLBACK_SKIP] {source_name} {error!r}")

    fallback_pool = list(_iter_local_search_seed_candidates(limit=max(limit * 5, 48)))
    return normalize_payload({
        "featured": fallback_pool[: min(limit, 10)],
        "manga": fallback_pool[:limit],
        "recommended": fallback_pool[limit : limit * 2] or fallback_pool[:limit],
        "top_viewed": fallback_pool[limit * 2 : limit * 3] or fallback_pool[:limit],
        "recent_chapter_read": fallback_pool[limit * 3 : limit * 4] or fallback_pool[:limit],
        "popular_season": fallback_pool[limit * 4 : limit * 5] or fallback_pool[:limit],
    })


async def _hybrid_get_recent_chapters(limit: int = AUTO_POST_LIMIT) -> list[dict[str, Any]]:
    target_limit = max(1, int(limit or AUTO_POST_LIMIT))
    primary_result = await _mangaball_get_recent_chapters(target_limit)
    primary = primary_result if isinstance(primary_result, list) else []
    secondary: list[dict[str, Any]] = []
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, items in (("mangaball", primary), ("mangafire", secondary)):
        for item in items:
            title_key = _hybrid_title_key(item) or _clean(item.get("title_id"))
            chapter_key = _hybrid_chapter_number(item.get("chapter_number") or item.get("latest_chapter"))
            key = f"{title_key}:{chapter_key}" if title_key and chapter_key else _clean(item.get("chapter_id"))
            if not key or key in seen:
                continue
            copy = dict(item)
            copy.setdefault("source", source)
            copy["sources"] = [source]
            seen.add(key)
            merged.append(copy)
            if len(merged) >= target_limit:
                return merged
    return merged


if _hybrid_enabled():
    from services import mangafire_client as _mangafire
    from services import mangadex_client as _mangadex
    from services import mangalivre_client as _mangalivre

    clear_catalog_cache = _mangaball_clear_catalog_cache
    get_csrf_token = _mangaball_get_csrf_token

    get_search_fallback_titles = _mangaball_get_search_fallback_titles
    get_cached_search_titles = _mangaball_get_cached_search_titles
    search_titles = _hybrid_search_titles
    search_titles_fast = _hybrid_search_titles_fast

    get_title_search = _mangaball_get_title_search
    get_cached_title_search = _mangaball_get_cached_title_search
    get_origin_titles = _mangaball_get_origin_titles
    get_home_payload = _hybrid_get_home_payload
    get_cached_home_snapshot = _mangaball_get_cached_home_snapshot
    get_recent_chapter_updates = _mangaball_get_recent_chapter_updates
    get_recent_chapters = _hybrid_get_recent_chapters

    get_cached_title_summary = _mangaball_get_cached_title_summary
    get_title_details = _hybrid_get_title_details
    get_title_overview = _hybrid_get_title_overview
    get_title_chapters_snapshot = _hybrid_get_title_chapters_snapshot
    get_title_bundle = _hybrid_get_title_bundle
    get_cached_title_bundle = _hybrid_get_cached_title_bundle

    get_chapter_list = _hybrid_get_chapter_list
    get_chapter_list_fast = _hybrid_get_chapter_list_fast
    get_cached_chapter_list = _mangaball_get_cached_chapter_list
    flatten_chapters = _hybrid_flatten_chapters
    get_adjacent_chapters = _hybrid_get_adjacent_chapters
    get_chapter_details = _hybrid_get_chapter_details
    get_chapter_reader_payload = _hybrid_get_chapter_reader_payload
    get_cached_chapter_reader_payload = _hybrid_get_cached_chapter_reader_payload

    warm_catalog_cache = _mangaball_warm_catalog_cache
    schedule_warm_catalog_cache = _mangaball_schedule_warm_catalog_cache
    prefetch_title_bundles = _mangaball_prefetch_title_bundles
    prefetch_reader_payloads = _mangaball_prefetch_reader_payloads
elif _use_mangafire_source():
    from services import mangafire_client as _mangafire

    clear_catalog_cache = _mangafire.clear_catalog_cache
    get_csrf_token = _mangafire.get_csrf_token

    get_search_fallback_titles = _mangafire.get_search_fallback_titles
    get_cached_search_titles = _mangafire.get_cached_search_titles
    search_titles = _mangafire.search_titles
    search_titles_fast = _mangafire.search_titles_fast

    get_title_search = _mangafire.get_title_search
    get_cached_title_search = _mangafire.get_cached_title_search
    get_origin_titles = _mangafire.get_origin_titles
    get_home_payload = _mangafire.get_home_payload
    get_cached_home_snapshot = _mangafire.get_cached_home_snapshot
    get_recent_chapter_updates = _mangafire.get_recent_chapter_updates
    get_recent_chapters = _mangafire.get_recent_chapters

    get_cached_title_summary = _mangafire.get_cached_title_summary
    get_title_details = _mangafire.get_title_details
    get_title_overview = _mangafire.get_title_overview
    get_title_chapters_snapshot = _mangafire.get_title_chapters_snapshot
    get_title_bundle = _mangafire.get_title_bundle
    get_cached_title_bundle = lambda title_ref, lang=None: None

    get_chapter_list = _mangafire.get_chapter_list
    get_chapter_list_fast = _mangafire.get_chapter_list_fast
    get_cached_chapter_list = _mangafire.get_cached_chapter_list
    flatten_chapters = _mangafire.flatten_chapters
    get_adjacent_chapters = _mangafire.get_adjacent_chapters
    get_chapter_details = _mangafire.get_chapter_details
    get_chapter_reader_payload = _mangafire.get_chapter_reader_payload
    get_cached_chapter_reader_payload = _mangafire.get_cached_chapter_reader_payload

    warm_catalog_cache = _mangafire.warm_catalog_cache
    schedule_warm_catalog_cache = _mangafire.schedule_warm_catalog_cache
    prefetch_title_bundles = _mangafire.prefetch_title_bundles
    prefetch_reader_payloads = _mangafire.prefetch_reader_payloads
