from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl
from threading import Lock

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import (
    ADMIN_IDS,
    BASE_DIR,
    BOT_BRAND,
    BOT_USERNAME,
    BOT_TOKEN,
    CAKTO_NOTIFY_USERS,
    CAKTO_REQUIRE_WEBHOOK_SECRET,
    CAKTO_WEBHOOK_SECRET,
    CACHE_CLEANUP_INTERVAL_SECONDS,
    CACHE_CLEANUP_STARTUP,
    DATA_DIR,
    DISTRIBUTION_TAG,
    HOME_SECTION_LIMIT,
    API_CACHE_MAX_ENTRIES,
    API_RATE_LIMIT_PER_MINUTE,
    PDF_BULK_ALLOWED_IDS,
    PDF_PROTECT_CONTENT,
    PREFERRED_CHAPTER_LANG,
    WEBAPP_CORS_ORIGINS,
    WEBAPP_TRUST_QUERY_USER_ID,
)
from services.catalog_client import (
    flatten_chapters,
    get_cached_title_bundle,
    get_cached_title_summary,
    get_chapter_reader_payload,
    get_home_payload,
    get_recent_chapters,
    get_title_bundle,
    get_title_chapters_snapshot,
    get_title_search,
    search_titles,
)
from services.cakto_gateway import extract_webhook_secret_values, process_cakto_webhook
from services.cache_cleanup import start_cache_cleanup_loop, stop_cache_cleanup_loop
from services.media_pipeline import resolve_telegraph_asset_path
from services.metrics import get_last_read_entry, get_recently_read, mark_chapter_read
from services.offline_access import init_offline_access_db, is_offline_user_allowed
from services.offline_messages import offline_welcome_message
from services.epub_service import get_or_build_epub
from services.pdf_service import get_or_build_pdf
from services.language_prefs import (
    bundle_language_options,
    get_user_language,
    language_options,
    normalize_language,
    set_user_language,
)
from services.affiliate_db import (
    admin_list_withdrawals,
    admin_list_affiliates,
    admin_overview,
    admin_user_snapshot,
    affiliate_summary,
    cents_to_money,
    complete_affiliate_account,
    get_profile,
    get_settings,
    init_affiliate_db,
    list_commissions,
    list_withdrawals,
    pay_withdrawal,
    refuse_withdrawal,
    release_due_commissions,
    request_withdrawal,
    set_pix_key,
    update_setting,
)
from services.profile_store import (
    list_user_favorites,
    merge_user_favorites,
    remove_user_favorite,
    set_user_favorite,
)

MINIAPP_DIR = BASE_DIR / "miniapp"
AFFILIATE_APP_DIR = MINIAPP_DIR / "affiliate"
PROGRESS_PATH = Path(DATA_DIR) / "miniapp_progress.json"
_PROGRESS_LOCK = Lock()

app = FastAPI(
    title="Mangas Baltigo API",
    description="API otimizada do miniapp de mangás",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=WEBAPP_CORS_ORIGINS or ["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

init_offline_access_db()
init_affiliate_db()


@app.on_event("startup")
async def _startup_cache_cleanup() -> None:
    first_delay = 0 if CACHE_CLEANUP_STARTUP else CACHE_CLEANUP_INTERVAL_SECONDS
    start_cache_cleanup_loop(first_delay=first_delay)


@app.on_event("shutdown")
async def _shutdown_cache_cleanup() -> None:
    await stop_cache_cleanup_loop()


class ProgressPayload(BaseModel):
    user_id: str = ""
    init_data: str = ""
    title_id: str = Field(min_length=1)
    title_name: str = ""
    chapter_id: str = Field(min_length=1)
    chapter_number: str = ""
    chapter_url: str = ""
    page_index: int = 0
    total_pages: int = 0
    cover_url: str = ""
    updated_at: int | float | str | None = None


class ProgressSyncPayload(BaseModel):
    user_id: str = ""
    init_data: str = ""
    progress: list[dict[str, Any]] = Field(default_factory=list)


class FavoritePayload(BaseModel):
    user_id: str = ""
    init_data: str = ""
    title_id: str = Field(min_length=1)
    title: str = ""
    display_title: str = ""
    cover_url: str = ""
    background_url: str = ""
    latest_chapter: Any = ""
    latest_chapter_id: Any = ""
    chapter_id: Any = ""
    chapter_number: Any = ""
    status: Any = ""
    anilist_score: Any = ""
    rating: Any = ""
    added_at: int | float | None = None
    updated_at: int | float | None = None
    favorite: bool = True


class PreferencesPayload(BaseModel):
    user_id: str = ""
    init_data: str = ""
    chapter_language: str = Field(min_length=1)


class ChapterDownloadPayload(BaseModel):
    user_id: str = ""
    init_data: str = ""
    chapter_id: str = Field(min_length=1)
    title_id: str = ""
    lang: str = ""
    format: str = "pdf"


class FavoritesSyncPayload(BaseModel):
    user_id: str = ""
    init_data: str = ""
    favorites: list[dict[str, Any]] = Field(default_factory=list)


class AffiliatePixPayload(BaseModel):
    user_id: str = ""
    init_data: str = ""
    pix_key: str = Field(min_length=3, max_length=180)


class AffiliateUserPayload(BaseModel):
    user_id: str = ""
    init_data: str = ""


class AffiliateAccountPayload(BaseModel):
    user_id: str = ""
    init_data: str = ""
    full_name: str = Field(min_length=3, max_length=160)
    email: str = Field(min_length=5, max_length=180)
    phone: str = Field(min_length=8, max_length=80)


class AffiliateAdminActionPayload(BaseModel):
    admin_user_id: str = ""
    init_data: str = ""
    note: str = ""


class AffiliateSettingPayload(BaseModel):
    admin_user_id: str = ""
    init_data: str = ""
    key: str = Field(min_length=1)
    value: str = Field(min_length=1, max_length=80)


_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = asyncio.Lock()
_RATE_LIMIT: dict[str, tuple[float, int]] = {}
_RATE_LIMIT_LOCK = asyncio.Lock()
_RECENT_TTL = 20
_HOME_TTL = 25
_TITLE_TTL = 90
_CHAPTER_TTL = 90
_SECTIONS_TTL = 25
_SEARCH_TTL = 20
_TITLE_OPEN_TIMEOUT = 22.0


def _now() -> float:
    return time.time()


def _safe_int_text(value: Any) -> str:
    raw = str(value or "").strip()
    return raw if raw.isdigit() else ""


def _validate_telegram_init_data(init_data: str) -> dict[str, Any]:
    raw = str(init_data or "").strip()
    if not raw:
        raise HTTPException(status_code=401, detail="MiniApp sem initData do Telegram.")
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="BOT_TOKEN nao configurado para validar MiniApp.")

    pairs = dict(parse_qsl(raw, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="initData sem assinatura.")

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="initData invalido.")

    auth_date = int(pairs.get("auth_date") or 0)
    if auth_date and time.time() - auth_date > 86400:
        raise HTTPException(status_code=401, detail="Sessao do MiniApp expirada.")

    try:
        user = json.loads(pairs.get("user") or "{}")
    except Exception as error:
        raise HTTPException(status_code=401, detail="Usuario do initData invalido.") from error
    if not isinstance(user, dict) or not _safe_int_text(user.get("id")):
        raise HTTPException(status_code=401, detail="Usuario do MiniApp nao identificado.")
    return {"user": user, "query": pairs}


def _request_init_data(request: Request, fallback: str = "") -> str:
    return (
        request.headers.get("x-telegram-init-data")
        or request.query_params.get("init_data")
        or fallback
        or ""
    )


def _authenticated_user_id(request: Request, claimed_user_id: Any = "", init_data: str = "") -> str:
    raw = _request_init_data(request, init_data)
    if raw:
        return str(_validate_telegram_init_data(raw)["user"]["id"])
    claimed = _safe_int_text(claimed_user_id)
    if WEBAPP_TRUST_QUERY_USER_ID and claimed:
        return claimed
    raise HTTPException(status_code=401, detail="Autenticacao do Telegram MiniApp obrigatoria.")


def _authenticated_admin_id(request: Request, claimed_user_id: Any = "", init_data: str = "") -> str:
    user_id = _authenticated_user_id(request, claimed_user_id, init_data)
    _admin_required(user_id)
    return user_id


def _cache_key(namespace: str, **kwargs: Any) -> str:
    raw = json.dumps({"ns": namespace, **kwargs}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


async def _cache_get(namespace: str, ttl: int, **kwargs: Any) -> Any | None:
    key = _cache_key(namespace, **kwargs)
    async with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        if entry["expires_at"] < _now():
            _CACHE.pop(key, None)
            return None
        return entry["value"]


async def _cache_set(namespace: str, value: Any, ttl: int, **kwargs: Any) -> Any:
    key = _cache_key(namespace, **kwargs)
    async with _CACHE_LOCK:
        _CACHE[key] = {
            "value": value,
            "expires_at": _now() + ttl,
        }
        if len(_CACHE) > max(100, API_CACHE_MAX_ENTRIES):
            expired_or_old = sorted(
                _CACHE.items(),
                key=lambda item: item[1].get("expires_at") or item[1].get("soft_expires_at") or 0,
            )
            for old_key, _ in expired_or_old[: max(1, len(_CACHE) - API_CACHE_MAX_ENTRIES)]:
                _CACHE.pop(old_key, None)
    return value


async def _cached(namespace: str, ttl: int, producer, **kwargs: Any) -> Any:
    cached = await _cache_get(namespace, ttl, **kwargs)
    if cached is not None:
        return cached
    value = await producer()
    return await _cache_set(namespace, value, ttl, **kwargs)


async def _stale_while_revalidate(namespace: str, ttl: int, stale_ttl: int, producer, **kwargs: Any) -> Any:
    key = _cache_key(namespace, **kwargs)
    async with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and entry["soft_expires_at"] >= _now():
            return entry["value"]
        if entry and entry["hard_expires_at"] >= _now():
            if not entry.get("refreshing"):
                entry["refreshing"] = True
                asyncio.create_task(_refresh_cache_entry(key, producer, ttl, stale_ttl))
            return entry["value"]

    value = await producer()
    async with _CACHE_LOCK:
        _CACHE[key] = {
            "value": value,
            "soft_expires_at": _now() + ttl,
            "hard_expires_at": _now() + stale_ttl,
            "refreshing": False,
        }
    return value


async def _refresh_cache_entry(key: str, producer, ttl: int, stale_ttl: int) -> None:
    try:
        value = await producer()
        async with _CACHE_LOCK:
            _CACHE[key] = {
                "value": value,
                "soft_expires_at": _now() + ttl,
                "hard_expires_at": _now() + stale_ttl,
                "refreshing": False,
            }
    except Exception:
        async with _CACHE_LOCK:
            if key in _CACHE:
                _CACHE[key]["refreshing"] = False


async def _invalidate_prefix(namespace: str) -> None:
    async with _CACHE_LOCK:
        for key in list(_CACHE.keys()):
            _CACHE.pop(key, None)


def _load_progress() -> dict[str, dict[str, Any]]:
    if not PROGRESS_PATH.exists():
        return {}
    try:
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_progress(data: dict[str, dict[str, Any]]) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = PROGRESS_PATH.with_suffix(PROGRESS_PATH.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, PROGRESS_PATH)


def _progress_key(user_id: str, title_id: str) -> str:
    return f"{user_id}:{title_id}"


def _public_last_read(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not entry:
        return None
    return {
        "title_id": entry.get("title_id") or "",
        "title_name": entry.get("title_name") or "",
        "chapter_id": entry.get("chapter_id") or "",
        "chapter_number": entry.get("chapter_number") or "",
        "updated_at": entry.get("updated_at") or "",
        "page_index": int(entry.get("page_index") or 0),
        "total_pages": int(entry.get("total_pages") or 0),
    }


def _public_updated_at_ms(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return int(time.time() * 1000)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            pass
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return int(time.time() * 1000)


def _public_history_item(user_id: str, item: dict[str, Any], progress_data: dict[str, dict[str, Any]]) -> dict[str, Any]:
    title_id = item.get("title_id") or ""
    progress = progress_data.get(_progress_key(user_id, title_id)) or {}
    page_index = int(progress.get("page_index") or 1)
    total_pages = int(progress.get("total_pages") or 0)

    return {
        "title_id": title_id,
        "title_name": item.get("title_name") or progress.get("title_name") or "",
        "chapter_id": item.get("chapter_id") or progress.get("chapter_id") or "",
        "chapter_number": item.get("chapter_number") or progress.get("chapter_number") or "",
        "chapter_url": item.get("chapter_url") or progress.get("chapter_url") or "",
        "page_index": page_index,
        "total_pages": total_pages,
        "cover_url": progress.get("cover_url") or "",
        "updated_at": _public_updated_at_ms(progress.get("updated_at") or item.get("updated_at")),
    }


def _public_chapter(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    return {
        "chapter_id": item.get("chapter_id") or "",
        "chapter_number": item.get("chapter_number") or "",
        "chapter_language": item.get("chapter_language") or "",
        "chapter_volume": item.get("chapter_volume") or "",
        "group_name": item.get("group_name") or "",
        "updated_at": item.get("updated_at") or "",
    }


def _has_real_chapter(item: dict[str, Any]) -> bool:
    return bool((item.get("chapter_id") or "").strip())


def _public_title_item(item: dict[str, Any]) -> dict[str, Any]:
    latest_value = item.get("latest_chapter")
    if isinstance(latest_value, dict):
        latest_value = latest_value.get("chapter_number") or latest_value.get("chapter_id") or ""

    return {
        "title_id": item.get("title_id") or "",
        "chapter_id": item.get("chapter_id") or "",
        "title": item.get("display_title") or item.get("title") or "",
        "cover_url": item.get("cover_url") or "",
        "background_url": item.get("background_url") or item.get("cover_url") or "",
        "status": item.get("status") or "",
        "rating": item.get("rating") or "",
        "updated_at": item.get("updated_at") or "",
        "latest_chapter": latest_value or "",
        "chapter_number": item.get("chapter_number") or latest_value or "",
        "adult": bool(item.get("adult")),
    }


def _sorted_filtered_chapters(bundle: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    chapters = flatten_chapters({"chapters": bundle.get("chapters") or []}, lang)
    clean = [c for c in chapters if _has_real_chapter(c)]

    def chapter_sort(item: dict[str, Any]) -> tuple[float, str]:
        raw = str(item.get("chapter_number") or "").strip()
        try:
            return (float(raw), item.get("updated_at") or "")
        except Exception:
            return (-1.0, item.get("updated_at") or "")

    clean.sort(key=chapter_sort, reverse=True)
    return clean


def _public_title_bundle(bundle: dict[str, Any], lang: str) -> dict[str, Any]:
    resolved_lang = normalize_language(lang) or PREFERRED_CHAPTER_LANG
    chapters = _sorted_filtered_chapters(bundle, resolved_lang)
    latest = next((item for item in chapters if item.get("chapter_id")), None)
    chapters_partial = bool(bundle.get("chapters_partial") or bundle.get("partial"))
    try:
        source_total_chapters = int(
            bundle.get("source_total_chapters")
            if bundle.get("source_total_chapters") not in (None, "")
            else bundle.get("total_chapters") or 0
        )
    except (TypeError, ValueError):
        source_total_chapters = 0

    try:
        estimated_total_chapters = int(bundle.get("anilist_chapters") or 0)
    except (TypeError, ValueError):
        estimated_total_chapters = 0

    total_chapters = len(chapters) or source_total_chapters

    return {
        "title_id": bundle.get("title_id") or "",
        "title": bundle.get("display_title") or bundle.get("title") or "",
        "preferred_title": bundle.get("preferred_title") or "",
        "alt_titles": bundle.get("alt_titles") or [],
        "description": bundle.get("description") or bundle.get("anilist_description") or "",
        "cover_url": bundle.get("cover_url") or "",
        "background_url": bundle.get("background_url") or bundle.get("cover_url") or "",
        "banner_url": bundle.get("banner_url") or bundle.get("background_url") or bundle.get("cover_url") or "",
        "cover_color": bundle.get("cover_color") or "",
        "status": bundle.get("status") or bundle.get("anilist_status") or "",
        "rating": bundle.get("rating") or "",
        "genres": bundle.get("genres") or [],
        "authors": bundle.get("authors") or [],
        "published": bundle.get("published") or "",
        "languages": bundle.get("languages") or [],
        "language_options": bundle_language_options(bundle),
        "current_language": resolved_lang,
        "total_chapters": total_chapters,
        "source_total_chapters": source_total_chapters,
        "estimated_total_chapters": estimated_total_chapters,
        "chapters_partial": chapters_partial,
        "metadata_partial": bool(bundle.get("metadata_partial")),
        "chapters_error": bundle.get("chapters_error") or bundle.get("error") or "",
        "anilist_url": bundle.get("anilist_url") or "",
        "anilist_score": bundle.get("anilist_score") or 0,
        "anilist_format": bundle.get("anilist_format") or "",
        "anilist_status": bundle.get("anilist_status") or "",
        "anilist_chapters": bundle.get("anilist_chapters") or 0,
        "anilist_volumes": bundle.get("anilist_volumes") or 0,
        "adult": bool(bundle.get("adult")),
        "chapters": [_public_chapter(item) for item in chapters],
        "latest_chapter": _public_chapter(latest or bundle.get("latest_chapter")),
    }


def _partial_title_payload(title_id: str, error: str = "") -> dict[str, Any]:
    summary = get_cached_title_summary(title_id) or {}
    latest = summary.get("latest_chapter")
    latest_chapter = None
    if isinstance(latest, dict):
        latest_chapter = latest
    elif summary.get("chapter_id"):
        latest_chapter = {
            "chapter_id": summary.get("chapter_id") or "",
            "chapter_number": str(latest or "").strip(),
            "chapter_language": summary.get("language") or PREFERRED_CHAPTER_LANG,
        }

    display_title = (
        summary.get("display_title")
        or summary.get("title")
        or "Manga"
    )
    cover_url = summary.get("cover_url") or ""
    try:
        source_total_chapters = int(
            summary.get("source_total_chapters")
            if summary.get("source_total_chapters") not in (None, "")
            else summary.get("chapters_count") or summary.get("chapter_count") or 0
        )
    except (TypeError, ValueError):
        source_total_chapters = 0

    return _public_title_bundle(
        {
            "title_id": title_id,
            "title": display_title,
            "display_title": display_title,
            "cover_url": cover_url,
            "background_url": summary.get("background_url") or cover_url,
            "status": summary.get("status") or summary.get("anilist_status") or "carregando",
            "rating": summary.get("rating") or summary.get("anilist_score") or "",
            "genres": summary.get("genres") or summary.get("anilist_genres") or [],
            "chapters": [],
            "languages": [],
            "total_chapters": source_total_chapters,
            "source_total_chapters": source_total_chapters,
            "anilist_chapters": summary.get("anilist_chapters") or 0,
            "latest_chapter": latest_chapter,
            "chapters_partial": True,
            "chapters_error": error,
        },
        PREFERRED_CHAPTER_LANG,
    )


def _public_reader_payload(payload: dict[str, Any]) -> dict[str, Any]:
    images = [img for img in (payload.get("images") or []) if str(img or "").strip()]
    return {
        "title_id": payload.get("title_id") or "",
        "title": payload.get("title") or "",
        "chapter_id": payload.get("chapter_id") or "",
        "chapter_number": payload.get("chapter_number") or "",
        "chapter_language": payload.get("chapter_language") or "",
        "chapter_volume": payload.get("chapter_volume") or "",
        "cover_url": payload.get("cover_url") or "",
        "image_count": len(images),
        "images": images,
        "total_chapters": payload.get("total_chapters") or 0,
        "previous_chapter": _public_chapter(payload.get("previous_chapter")),
        "next_chapter": _public_chapter(payload.get("next_chapter")),
    }


def _normalize_query(text: str) -> str:
    import re
    import unicodedata

    text = unicodedata.normalize("NFKD", (text or "").strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _search_score(query: str, item: dict[str, Any]) -> tuple[int, int, int]:
    q = _normalize_query(query)
    title = _normalize_query(item.get("title") or item.get("preferred_title") or item.get("display_title") or "")
    tags = [_normalize_query(tag) for tag in (item.get("genres") or [])]

    if not q:
        return (0, 0, 0)
    if title == q:
        return (500, 0, -len(title))
    if title.startswith(q):
        return (400, 0, -len(title))
    if q in title:
        return (300, 0, -len(title))
    if any(q in tag for tag in tags):
        return (220, 0, -len(title))

    overlap = len(set(q.split()) & set(title.split()))
    return (100 + overlap * 10, 0, -len(title))


async def _search_with_suggestions(query: str, limit: int) -> dict[str, Any]:
    raw_results = await _cached(
        "search",
        _SEARCH_TTL,
        lambda: search_titles(query, limit=max(20, limit * 3)),
        query=query,
        limit=max(20, limit * 3),
    )

    candidates = []
    for item in raw_results:
        if not item.get("title_id"):
            continue
        candidates.append(item)

    ranked = sorted(candidates, key=lambda item: _search_score(query, item), reverse=True)
    ranked = ranked[:limit]

    if ranked:
        return {
            "query": query,
            "results": [_public_title_item(item) for item in ranked],
            "suggestions": [],
        }

    home = await _home_payload(limit=max(10, limit))
    pool = []
    for key in ("featured", "popular", "recent_titles", "latest_titles"):
        pool.extend(home.get(key) or [])

    seen: set[str] = set()
    dedup_pool = []
    for item in pool:
        title_id = item.get("title_id") or ""
        if not title_id or title_id in seen:
            continue
        seen.add(title_id)
        dedup_pool.append(item)

    suggestions = sorted(dedup_pool, key=lambda item: _search_score(query, item), reverse=True)[:6]
    return {
        "query": query,
        "results": [],
        "suggestions": [_public_title_item(item) for item in suggestions if item.get("title_id")],
    }


async def _home_payload(limit: int) -> dict[str, Any]:
    async def producer() -> dict[str, Any]:
        payload, recent_chapters = await asyncio.gather(
            get_home_payload(limit=limit),
            get_recent_chapters(limit=min(limit * 2, 24)),
        )

        featured = [_public_title_item(item) for item in (payload.get("featured") or []) if _has_real_chapter(item)]
        popular = [_public_title_item(item) for item in (payload.get("popular") or []) if _has_real_chapter(item)]
        recent_titles = [_public_title_item(item) for item in (payload.get("recent_titles") or []) if _has_real_chapter(item)]
        latest_titles = [_public_title_item(item) for item in (payload.get("latest_titles") or []) if _has_real_chapter(item)]

        public_recent_chapters = []
        seen_chapters: set[str] = set()
        for item in recent_chapters:
            chapter_id = item.get("chapter_id") or ""
            if not chapter_id or chapter_id in seen_chapters:
                continue
            seen_chapters.add(chapter_id)
            public_recent_chapters.append(_public_title_item(item))

        latest_titles.sort(
            key=lambda item: (item.get("updated_at") or "", item.get("latest_chapter") or ""),
            reverse=True,
        )
        public_recent_chapters.sort(
            key=lambda item: (
                item.get("updated_at") or "",
                item.get("chapter_number") or item.get("latest_chapter") or "",
            ),
            reverse=True,
        )

        return {
            "featured": featured[:limit],
            "popular": popular[:limit],
            "recent_titles": recent_titles[:limit],
            "latest_titles": latest_titles[:limit],
            "recent_chapters": public_recent_chapters[: max(limit, 12)],
        }

    return await _stale_while_revalidate("home", _HOME_TTL, _HOME_TTL * 3, producer, limit=limit)


async def _title_payload(title_id: str, lang: str, user_id: str = "") -> dict[str, Any]:
    cache_kwargs = {"title_id": title_id, "lang": lang, "user_id": user_id}
    cached = await _cache_get("title", _TITLE_TTL, **cache_kwargs)
    if cached is not None and not cached.get("chapters_partial") and not cached.get("metadata_partial"):
        return cached

    def attach_user_data(public_bundle: dict[str, Any]) -> dict[str, Any]:
        if user_id:
            public_bundle["last_read"] = _public_last_read(get_last_read_entry(user_id, public_bundle["title_id"]))
        return public_bundle

    def refresh_full_bundle() -> None:
        async def runner() -> None:
            try:
                await get_title_bundle(title_id, lang)
            except Exception as error:
                print("[WEBAPP][TITLE_REFRESH_FAIL]", title_id, repr(error))

        try:
            asyncio.create_task(runner())
        except RuntimeError:
            pass

    catalog_cached = get_cached_title_bundle(title_id, lang)
    if catalog_cached is not None and catalog_cached.get("chapters"):
        public_cached = attach_user_data(_public_title_bundle(catalog_cached, lang))
        if public_cached.get("metadata_partial"):
            refresh_full_bundle()
            return public_cached
        return await _cache_set("title", public_cached, _TITLE_TTL, **cache_kwargs)

    async def producer() -> dict[str, Any]:
        try:
            snapshot = await asyncio.wait_for(
                get_title_chapters_snapshot(title_id, lang),
                timeout=min(_TITLE_OPEN_TIMEOUT, 4.5),
            )
            if snapshot.get("chapters"):
                refresh_full_bundle()
                return attach_user_data(_public_title_bundle(snapshot, lang))
        except Exception as error:
            print("[WEBAPP][TITLE_SNAPSHOT_PARTIAL]", title_id, repr(error))

        try:
            bundle = await asyncio.wait_for(
                get_title_bundle(title_id, lang),
                timeout=_TITLE_OPEN_TIMEOUT,
            )
        except Exception as error:
            print("[WEBAPP][TITLE_PARTIAL]", title_id, repr(error))
            return _partial_title_payload(title_id, repr(error))

        return attach_user_data(_public_title_bundle(bundle, lang))

    value = await producer()
    if value.get("chapters_partial") or value.get("metadata_partial"):
        return value
    return await _cache_set("title", value, _TITLE_TTL, **cache_kwargs)


async def _chapter_payload(chapter_id: str, lang: str) -> dict[str, Any]:
    async def producer() -> dict[str, Any]:
        payload = await get_chapter_reader_payload(chapter_id, lang)
        return _public_reader_payload(payload)

    return await _cached("chapter", _CHAPTER_TTL, producer, chapter_id=chapter_id, lang=lang)


def _can_download_from_webapp(user_id: str | int | None) -> bool:
    try:
        uid = int(str(user_id or "").strip())
    except (TypeError, ValueError):
        return False
    return uid in set(PDF_BULK_ALLOWED_IDS) or is_offline_user_allowed(uid)


def _download_caption(chapter: dict[str, Any]) -> str:
    title = html.escape(chapter.get("title") or "Manga")
    number = html.escape(str(chapter.get("chapter_number") or "?"))
    tag = html.escape(DISTRIBUTION_TAG or "")
    return (
        f"<b>{title}</b>\n"
        f"Capítulo <code>{number}</code>\n"
        f"{tag}"
    ).strip()


def _download_mime(format_name: str) -> str:
    return "application/epub+zip" if format_name == "epub" else "application/pdf"


async def _telegram_api(method: str, *, data: dict[str, Any] | None = None, files: dict[str, Any] | None = None) -> dict[str, Any]:
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="BOT_TOKEN não configurado para enviar o arquivo.")
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            data=data or {},
            files=files,
        )
    try:
        payload = response.json()
    except Exception:
        payload = {"ok": False, "description": response.text}
    if response.status_code >= 400 or not payload.get("ok"):
        detail = payload.get("description") or f"Telegram API HTTP {response.status_code}"
        raise HTTPException(status_code=502, detail=detail)
    return payload


async def _telegram_edit_message(chat_id: int, message_id: int, text: str) -> None:
    try:
        await _telegram_api(
            "editMessageText",
            data={
                "chat_id": str(chat_id),
                "message_id": str(message_id),
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
    except Exception:
        pass


async def _run_chapter_download_job(
    *,
    chat_id: int,
    message_id: int,
    chapter: dict[str, Any],
    format_name: str,
) -> None:
    label = "EPUB" if format_name == "epub" else "PDF"

    async def progress(done: int, total: int) -> None:
        if not message_id:
            return
        pct = int((done / max(total, 1)) * 100)
        await _telegram_edit_message(
            chat_id,
            message_id,
            (
                f"📥 <b>Gerando {label}</b>\n\n"
                f"Obra: <i>{html.escape(chapter.get('title') or 'Manga')}</i>\n"
                f"Capítulo: <i>{html.escape(str(chapter.get('chapter_number') or '?'))}</i>\n"
                f"Progresso: <b>{pct}%</b>"
            ),
        )

    try:
        if format_name == "epub":
            file_path, file_name = await get_or_build_epub(
                chapter_id=chapter["chapter_id"],
                chapter_number=chapter.get("chapter_number") or "?",
                title_name=chapter.get("title") or "Manga",
                images=chapter.get("images") or [],
                progress_cb=progress,
            )
        else:
            file_path, file_name = await get_or_build_pdf(
                chapter_id=chapter["chapter_id"],
                chapter_number=chapter.get("chapter_number") or "?",
                title_name=chapter.get("title") or "Manga",
                images=chapter.get("images") or [],
                progress_cb=progress,
            )

        with open(file_path, "rb") as file:
            await _telegram_api(
                "sendDocument",
                data={
                    "chat_id": str(chat_id),
                    "caption": _download_caption(chapter),
                    "parse_mode": "HTML",
                    "protect_content": "true" if PDF_PROTECT_CONTENT else "false",
                },
                files={"document": (file_name, file, _download_mime(format_name))},
            )
        if message_id:
            await _telegram_edit_message(
                chat_id,
                message_id,
                (
                    f"✅ <b>{label} enviado</b>\n\n"
                    f"Obra: <i>{html.escape(chapter.get('title') or 'Manga')}</i>\n"
                    f"Capítulo: <i>{html.escape(str(chapter.get('chapter_number') or '?'))}</i>"
                ),
            )
    except Exception as error:
        if message_id:
            await _telegram_edit_message(
                chat_id,
                message_id,
                f"❌ <b>Não consegui gerar o {label}.</b>\n\n<code>{html.escape(str(error))}</code>",
            )


async def _run_chapter_download_request(
    *,
    chat_id: int,
    chapter_id: str,
    lang: str,
    format_name: str,
) -> None:
    label = "EPUB" if format_name == "epub" else "PDF"
    message_id = 0
    try:
        status = await _telegram_api(
            "sendMessage",
            data={
                "chat_id": str(chat_id),
                "text": (
                    "📥 <b>Download iniciado pelo webapp</b>\n\n"
                    f"Formato: <b>{label}</b>\n\n"
                    "Pode continuar escolhendo outros capítulos. Vou enviar o arquivo aqui quando ficar pronto."
                ),
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        message_id = int((status.get("result") or {}).get("message_id") or 0)
        chapter = await _chapter_payload(chapter_id, lang)
        images = chapter.get("images") or []
        if not images:
            if message_id:
                await _telegram_edit_message(
                    chat_id,
                    message_id,
                    f"❌ <b>Não encontrei imagens para gerar esse {label}.</b>",
                )
            return
        await _run_chapter_download_job(
            chat_id=chat_id,
            message_id=message_id,
            chapter=chapter,
            format_name=format_name,
        )
    except Exception as error:
        if message_id:
            await _telegram_edit_message(
                chat_id,
                message_id,
                f"❌ <b>Não consegui iniciar o {label}.</b>\n\n<code>{html.escape(str(error))}</code>",
            )


@app.get("/api/ping")
async def ping() -> dict[str, bool]:
    return {"ok": True}


def _cakto_secret_candidates(request: Request, payload: dict[str, Any]) -> list[str]:
    candidates: list[str] = []

    for key in ("secret", "token"):
        value = request.query_params.get(key)
        if value:
            candidates.append(value.strip())

    for header_name in (
        "x-cakto-secret",
        "x-webhook-secret",
        "x-secret",
        "x-cakto-token",
    ):
        value = request.headers.get(header_name)
        if value:
            candidates.append(value.strip())

    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        candidates.append(authorization.split(" ", 1)[1].strip())
    elif authorization:
        candidates.append(authorization)

    candidates.extend(extract_webhook_secret_values(payload))
    return [item for item in candidates if item]


def _cakto_secret_is_valid(request: Request, payload: dict[str, Any]) -> bool:
    expected = (CAKTO_WEBHOOK_SECRET or "").strip()
    if not expected:
        return not CAKTO_REQUIRE_WEBHOOK_SECRET
    return expected in _cakto_secret_candidates(request, payload)


def _is_admin_user(user_id: str | int | None) -> bool:
    try:
        return int(user_id or 0) in set(ADMIN_IDS)
    except Exception:
        return False


def _admin_required(user_id: str | int | None) -> None:
    if not _is_admin_user(user_id):
        raise HTTPException(status_code=403, detail="Acesso admin negado.")


def _money_fields(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    for key in ("amount_cents", "commission_amount_cents", "sale_amount_cents", "available_cents", "pending_cents", "paid_cents", "withdrawal_pending_cents", "canceled_cents"):
        if key in result:
            result[key.replace("_cents", "") + "_formatted"] = cents_to_money(result.get(key))
    return result


async def _notify_cakto_user(result: dict[str, Any]) -> None:
    if not CAKTO_NOTIFY_USERS or not BOT_TOKEN:
        return

    access = result.get("access") or {}
    if access.get("duplicate_event"):
        return

    user_id = result.get("user_id")
    if not user_id:
        return

    action = result.get("action")
    brand = html.escape(BOT_BRAND or "Mangas Baltigo")

    if action == "granted":
        plan = html.escape(result.get("plan_label") or access.get("plan_label") or "plano")
        expires_at = access.get("expires_at") or "vitalício"
        text = (
            "✅ <b>Leitura offline liberada!</b>\n\n"
            f"» <b>Plano:</b> <i>{plan}</i>\n"
            f"» <b>Validade:</b> <i>{html.escape(str(expires_at))}</i>\n\n"
            f"Agora o envio de todos os capítulos em PDF está ativo no <b>{brand}</b>."
        )
    elif action == "revoked":
        text = (
            "🔒 <b>Leitura offline bloqueada</b>\n\n"
            "A Cakto avisou cancelamento, reembolso ou chargeback dessa assinatura."
        )
    else:
        return

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": int(user_id),
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except Exception:
        pass


async def _notify_cakto_user(result: dict[str, Any]) -> None:
    if not CAKTO_NOTIFY_USERS or not BOT_TOKEN:
        return

    access = result.get("access") or {}
    if access.get("duplicate_event"):
        return

    user_id = result.get("user_id")
    if not user_id:
        return

    action = result.get("action")
    if action == "granted":
        text = offline_welcome_message(access or result, source="payment")
    elif action == "revoked":
        text = (
            "🔒 <b>Leitura offline bloqueada</b>\n\n"
            "Recebemos um aviso de cancelamento, reembolso ou chargeback dessa assinatura.\n\n"
            "Se você acredita que isso foi um engano, fale com o suporte."
        )
    else:
        return

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": int(user_id),
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except Exception:
        pass


def _log_cakto_webhook_payload(payload: dict[str, Any], result: dict[str, Any] | None = None) -> None:
    path = Path(DATA_DIR) / "cakto_webhooks.jsonl"
    redacted_payload = dict(payload)
    for key in list(redacted_payload.keys()):
        if any(token in str(key).lower() for token in ("secret", "token", "password", "pix", "document")):
            redacted_payload[key] = "[redacted]"
    record = {
        "received_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "result": result or {},
        "payload": redacted_payload,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


@app.post("/api/webhooks/cakto")
async def api_cakto_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="JSON inválido.") from error

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload do webhook precisa ser JSON object.")

    if not _cakto_secret_is_valid(request, payload):
        _log_cakto_webhook_payload(payload, {"action": "unauthorized"})
        raise HTTPException(status_code=401, detail="Webhook Cakto não autorizado.")

    result = process_cakto_webhook(payload)
    _log_cakto_webhook_payload(payload, result)
    if result.get("action") in {"granted", "revoked"}:
        asyncio.create_task(_notify_cakto_user(result))

    return result


@app.get("/affiliate")
async def affiliate_root():
    return FileResponse(AFFILIATE_APP_DIR / "index.html")


@app.get("/affiliate/")
async def affiliate_root_slash():
    return FileResponse(AFFILIATE_APP_DIR / "index.html")


@app.get("/app/affiliate")
async def affiliate_app_alias():
    return FileResponse(AFFILIATE_APP_DIR / "index.html")


@app.get("/app/affiliate/")
async def affiliate_app_alias_slash():
    return FileResponse(AFFILIATE_APP_DIR / "index.html")


@app.get("/affiliate/share/{user_id}")
async def affiliate_share_preview(user_id: int):
    bot_username = BOT_USERNAME or "MangasBaltigo_Bot"
    ref_url = f"https://t.me/{bot_username}?start=ref_{int(user_id)}"
    image_url = (
        "https://photo.chelpbot.me/AgACAgEAAxkBZ7DGAAFpse3x62wh4yTxu0BIhIPz12L_YwACMAxrGxpikUXp6-kJkxw_1QEAAwIAA3kAAzoE/photo.jpg"
    )
    html_body = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Baltigo - Mangas no Telegram</title>
  <meta property="og:title" content="Baltigo - Mangas direto no Telegram">
  <meta property="og:description" content="Leia mangas, acompanhe capitulos e libere recursos offline pelo bot Baltigo.">
  <meta property="og:image" content="{image_url}">
  <meta property="og:type" content="website">
  <meta name="twitter:card" content="summary_large_image">
  <meta http-equiv="refresh" content="0; url={ref_url}">
  <style>
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#080b12; color:#fff; font-family:system-ui,sans-serif; }}
    a {{ color:#93c5fd; }}
  </style>
</head>
<body>
  <main>
    <h1>Baltigo</h1>
    <p>Redirecionando para o bot...</p>
    <a href="{ref_url}">Abrir agora</a>
  </main>
</body>
</html>"""
    return Response(content=html_body, media_type="text/html")


@app.get("/api/affiliate/summary")
async def api_affiliate_summary(request: Request, user_id: str = Query("")):
    user_id = _authenticated_user_id(request, user_id)
    summary = affiliate_summary(user_id)
    summary["available_formatted"] = cents_to_money(summary.get("available_cents"))
    summary["pending_formatted"] = cents_to_money(summary.get("pending_cents"))
    summary["paid_formatted"] = cents_to_money(summary.get("paid_cents"))
    summary["withdrawal_pending_formatted"] = cents_to_money(summary.get("withdrawal_pending_cents"))
    summary["canceled_formatted"] = cents_to_money(summary.get("canceled_cents"))
    summary["is_admin"] = _is_admin_user(user_id)
    return summary


@app.get("/api/affiliate/commissions")
async def api_affiliate_commissions(request: Request, user_id: str = Query(""), limit: int = Query(60, ge=1, le=200)):
    user_id = _authenticated_user_id(request, user_id)
    return {"items": [_money_fields(item) for item in list_commissions(user_id, limit=limit)]}


@app.get("/api/affiliate/withdrawals")
async def api_affiliate_withdrawals(request: Request, user_id: str = Query(""), limit: int = Query(40, ge=1, le=200)):
    user_id = _authenticated_user_id(request, user_id)
    return {"items": [_money_fields(item) for item in list_withdrawals(user_id, limit=limit)]}


@app.post("/api/affiliate/pix")
async def api_affiliate_pix(request: Request, payload: AffiliatePixPayload):
    user_id = _authenticated_user_id(request, payload.user_id, payload.init_data)
    return {"ok": True, "profile": set_pix_key(user_id, payload.pix_key)}


@app.post("/api/affiliate/account")
async def api_affiliate_account(request: Request, payload: AffiliateAccountPayload):
    user_id = _authenticated_user_id(request, payload.user_id, payload.init_data)
    try:
        profile = complete_affiliate_account(user_id, payload.full_name, payload.email, payload.phone)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "profile": profile}


@app.post("/api/affiliate/withdrawals")
async def api_affiliate_request_withdrawal(request: Request, payload: AffiliateUserPayload):
    user_id = _authenticated_user_id(request, payload.user_id, payload.init_data)
    try:
        withdrawal = request_withdrawal(user_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "withdrawal": _money_fields(withdrawal)}


@app.post("/api/affiliate/release")
async def api_affiliate_release(request: Request, payload: AffiliateAdminActionPayload):
    _authenticated_admin_id(request, payload.admin_user_id, payload.init_data)
    return {"ok": True, "released": release_due_commissions()}


@app.get("/api/affiliate/admin/overview")
async def api_affiliate_admin_overview(request: Request, admin_user_id: str = Query("")):
    _authenticated_admin_id(request, admin_user_id)
    return admin_overview()


@app.get("/api/affiliate/admin/withdrawals")
async def api_affiliate_admin_withdrawals(
    request: Request,
    admin_user_id: str = Query(""),
    status: str = Query("pending"),
    limit: int = Query(100, ge=1, le=300),
):
    _authenticated_admin_id(request, admin_user_id)
    return {"items": [_money_fields(item) for item in admin_list_withdrawals(status=status, limit=limit)]}


@app.get("/api/affiliate/admin/affiliates")
async def api_affiliate_admin_affiliates(
    request: Request,
    admin_user_id: str = Query(""),
    q: str = Query(""),
    tier: str = Query("all"),
    status: str = Query("all"),
    sort: str = Query("sales"),
    limit: int = Query(200, ge=1, le=500),
):
    _authenticated_admin_id(request, admin_user_id)
    return {
        "items": [
            _money_fields(item)
            for item in admin_list_affiliates(query=q, tier=tier, status=status, sort=sort, limit=limit)
        ]
    }


@app.get("/api/affiliate/admin/user/{user_id}")
async def api_affiliate_admin_user(request: Request, user_id: str, admin_user_id: str = Query("")):
    _authenticated_admin_id(request, admin_user_id)
    return admin_user_snapshot(user_id)


@app.post("/api/affiliate/admin/withdrawals/{withdrawal_id}/pay")
async def api_affiliate_admin_pay_withdrawal(request: Request, withdrawal_id: int, payload: AffiliateAdminActionPayload):
    admin_id = _authenticated_admin_id(request, payload.admin_user_id, payload.init_data)
    try:
        withdrawal = pay_withdrawal(withdrawal_id, admin_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "withdrawal": _money_fields(withdrawal)}


@app.post("/api/affiliate/admin/withdrawals/{withdrawal_id}/refuse")
async def api_affiliate_admin_refuse_withdrawal(request: Request, withdrawal_id: int, payload: AffiliateAdminActionPayload):
    _authenticated_admin_id(request, payload.admin_user_id, payload.init_data)
    try:
        withdrawal = refuse_withdrawal(withdrawal_id, payload.note)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "withdrawal": _money_fields(withdrawal)}


@app.get("/api/affiliate/settings")
async def api_affiliate_settings(request: Request, admin_user_id: str = Query("")):
    _authenticated_admin_id(request, admin_user_id)
    return {"settings": get_settings()}


@app.post("/api/affiliate/settings")
async def api_affiliate_update_setting(request: Request, payload: AffiliateSettingPayload):
    _authenticated_admin_id(request, payload.admin_user_id, payload.init_data)
    try:
        update_setting(payload.key, payload.value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "settings": get_settings()}


@app.get("/api/home")
async def api_home(limit: int = Query(HOME_SECTION_LIMIT, ge=4, le=24)):
    return await _home_payload(limit=limit)


@app.get("/api/search")
async def api_search(q: str = Query("", min_length=1), limit: int = Query(12, ge=1, le=24)):
    return await _search_with_suggestions(q, limit)


@app.get("/api/sections/{section_name}")
async def api_section(section_name: str, limit: int = Query(12, ge=1, le=24)):
    async def producer() -> dict[str, Any]:
        if section_name == "recent_chapters":
            items = await get_recent_chapters(limit=max(limit, 12))
            clean = [_public_title_item(item) for item in items if _has_real_chapter(item)]
            clean.sort(
                key=lambda item: (
                    item.get("updated_at") or "",
                    item.get("chapter_number") or item.get("latest_chapter") or "",
                ),
                reverse=True,
            )
            return {"items": clean[:limit]}

        section_map = {
            "featured": "getFeatured",
            "popular": "getPopular",
            "recent_titles": "getRecentRead",
            "latest_titles": "getLatestTable",
        }
        search_type = section_map.get(section_name)
        if not search_type:
            raise HTTPException(status_code=404, detail="Seção não encontrada.")

        extra = {"search_time": "week"} if search_type == "getRecentRead" else {}
        items = await get_title_search(search_type, limit=max(limit, 16), **extra)
        clean = [_public_title_item(item) for item in items if _has_real_chapter(item)]
        if section_name in {"latest_titles", "recent_titles"}:
            clean.sort(
                key=lambda item: (item.get("updated_at") or "", item.get("latest_chapter") or ""),
                reverse=True,
            )
        return {"items": clean[:limit]}

    return await _stale_while_revalidate(
        "section",
        _SECTIONS_TTL,
        _SECTIONS_TTL * 3,
        producer,
        section_name=section_name,
        limit=limit,
    )


@app.get("/api/title/{title_id}")
async def api_title(request: Request, title_id: str, user_id: str = Query(""), lang: str = Query("")):
    try:
        safe_user_id = ""
        if user_id or _request_init_data(request):
            safe_user_id = _authenticated_user_id(request, user_id)
        resolved_lang = normalize_language(lang) or get_user_language(safe_user_id, PREFERRED_CHAPTER_LANG)
        return await _title_payload(title_id, resolved_lang, safe_user_id)
    except Exception as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/title/{title_id}/chapters")
async def api_title_chapters(title_id: str, lang: str = Query(PREFERRED_CHAPTER_LANG)):
    try:
        resolved_lang = normalize_language(lang) or PREFERRED_CHAPTER_LANG
        bundle = await _title_payload(title_id, resolved_lang)
        return {
            "title_id": bundle["title_id"],
            "title": bundle.get("title") or "",
            "chapters": bundle.get("chapters") or [],
            "language_options": bundle.get("language_options") or [],
            "current_language": bundle.get("current_language") or resolved_lang,
        }
    except Exception as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/chapter/{chapter_id}")
async def api_chapter(request: Request, chapter_id: str, user_id: str = Query(""), lang: str = Query("")):
    try:
        safe_user_id = ""
        if user_id or _request_init_data(request):
            safe_user_id = _authenticated_user_id(request, user_id)
        resolved_lang = normalize_language(lang) or get_user_language(safe_user_id, PREFERRED_CHAPTER_LANG)
        return await _chapter_payload(chapter_id, resolved_lang)
    except Exception as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/download/chapter")
async def api_download_chapter(request: Request, background_tasks: BackgroundTasks, payload: ChapterDownloadPayload):
    try:
        user_id = _authenticated_user_id(request, payload.user_id, payload.init_data)
    except HTTPException:
        user_id = _safe_int_text(payload.user_id)
        if not user_id:
            raise
    if not _can_download_from_webapp(user_id):
        raise HTTPException(
            status_code=403,
            detail=(
                "Seu acesso offline ainda não está ativo. Abra o chat do bot e envie /plano "
                "para escolher um plano. Se um administrador liberou você manualmente com /liberar, "
                "feche e abra o webapp novamente."
            ),
        )

    format_name = str(payload.format or "pdf").strip().lower()
    if format_name not in {"pdf", "epub"}:
        raise HTTPException(status_code=400, detail="Formato inválido. Escolha PDF ou EPUB.")
    label = "EPUB" if format_name == "epub" else "PDF"
    chat_id = int(user_id)
    resolved_lang = normalize_language(payload.lang) or get_user_language(user_id, PREFERRED_CHAPTER_LANG)
    background_tasks.add_task(
        _run_chapter_download_request,
        chat_id=chat_id,
        chapter_id=payload.chapter_id,
        lang=resolved_lang,
        format_name=format_name,
    )

    return {
        "ok": True,
        "message": f"Pedido recebido. O {label} será enviado no chat do bot.",
        "chapter_id": payload.chapter_id,
        "format": format_name,
    }


@app.get("/api/preferences")
async def api_get_preferences(request: Request, user_id: str = Query("")):
    user_id = _authenticated_user_id(request, user_id)
    lang = get_user_language(user_id, PREFERRED_CHAPTER_LANG)
    return {
        "chapter_language": lang,
        "language_options": language_options([lang]),
    }


@app.post("/api/preferences")
async def api_save_preferences(request: Request, payload: PreferencesPayload):
    user_id = _authenticated_user_id(request, payload.user_id, payload.init_data)
    preference = set_user_language(user_id, payload.chapter_language)
    await _invalidate_prefix("cache")
    return {"ok": True, "preferences": preference}


@app.get("/api/progress")
async def api_get_progress(request: Request, user_id: str = Query(""), title_id: str = Query(...)):
    user_id = _authenticated_user_id(request, user_id)
    data = _load_progress()
    return _public_last_read(data.get(_progress_key(user_id, title_id))) or {}


@app.get("/api/history")
async def api_get_history(request: Request, user_id: str = Query(""), limit: int = Query(80, ge=1, le=200)):
    user_id = _authenticated_user_id(request, user_id)
    progress_data = _load_progress()
    items = get_recently_read(user_id, limit=limit)
    return {
        "items": [
            _public_history_item(user_id, item, progress_data)
            for item in items
            if item.get("title_id") and item.get("chapter_id")
        ]
    }


@app.post("/api/progress")
async def api_save_progress(request: Request, payload: ProgressPayload):
    user_id = _authenticated_user_id(request, payload.user_id, payload.init_data)
    data = _load_progress()
    key = _progress_key(user_id, payload.title_id)
    stored = payload.model_dump()
    if not stored.get("updated_at"):
        stored["updated_at"] = int(time.time() * 1000)
    data[key] = stored
    _save_progress(data)

    mark_chapter_read(
        user_id=user_id,
        title_id=payload.title_id,
        chapter_id=payload.chapter_id,
        chapter_number=payload.chapter_number,
        title_name=payload.title_name,
        chapter_url=payload.chapter_url,
    )

    await _invalidate_prefix("cache")
    return {"ok": True}


@app.post("/api/progress/sync")
async def api_sync_progress(request: Request, payload: ProgressSyncPayload):
    user_id = _authenticated_user_id(request, payload.user_id, payload.init_data)
    data = _load_progress()
    now_ms = int(time.time() * 1000)

    for raw_item in (payload.progress or [])[:200]:
        if not isinstance(raw_item, dict):
            continue

        title_id = str(raw_item.get("title_id") or "").strip()
        chapter_id = str(raw_item.get("chapter_id") or "").strip()
        if not title_id or not chapter_id:
            continue

        key = _progress_key(user_id, title_id)
        current = data.get(key) or {}
        incoming_updated = _public_updated_at_ms(raw_item.get("updated_at") or now_ms)
        current_updated = _public_updated_at_ms(current.get("updated_at")) if current else 0
        if current and incoming_updated < current_updated:
            continue

        record = {
            "user_id": user_id,
            "title_id": title_id,
            "title_name": str(raw_item.get("title_name") or raw_item.get("title") or "").strip(),
            "chapter_id": chapter_id,
            "chapter_number": str(raw_item.get("chapter_number") or "").strip(),
            "chapter_url": str(raw_item.get("chapter_url") or "").strip(),
            "page_index": int(raw_item.get("page_index") or 0),
            "total_pages": int(raw_item.get("total_pages") or 0),
            "cover_url": str(raw_item.get("cover_url") or "").strip(),
            "updated_at": incoming_updated,
        }
        data[key] = {**current, **record}

        try:
            mark_chapter_read(
                user_id=user_id,
                title_id=title_id,
                chapter_id=chapter_id,
                chapter_number=record["chapter_number"],
                title_name=record["title_name"],
                chapter_url=record["chapter_url"],
            )
        except Exception:
            pass

    _save_progress(data)
    items = get_recently_read(user_id, limit=200)
    return {
        "ok": True,
        "items": [
            _public_history_item(user_id, item, data)
            for item in items
            if item.get("title_id") and item.get("chapter_id")
        ],
    }


@app.get("/api/favorites")
async def api_get_favorites(request: Request, user_id: str = Query("")):
    user_id = _authenticated_user_id(request, user_id)
    return {"items": list_user_favorites(user_id, limit=200)}


@app.post("/api/favorites")
async def api_save_favorite(request: Request, payload: FavoritePayload):
    user_id = _authenticated_user_id(request, payload.user_id, payload.init_data)
    if not payload.favorite:
        remove_user_favorite(user_id, payload.title_id)
        return {"ok": True, "items": list_user_favorites(user_id, limit=200)}

    favorite = payload.model_dump(exclude={"favorite", "user_id"})
    set_user_favorite(user_id, favorite)
    return {"ok": True, "items": list_user_favorites(user_id, limit=200)}


@app.post("/api/favorites/sync")
async def api_sync_favorites(request: Request, payload: FavoritesSyncPayload):
    user_id = _authenticated_user_id(request, payload.user_id, payload.init_data)
    return {"ok": True, "items": merge_user_favorites(user_id, payload.favorites)}


@app.post("/api/refresh")
async def api_refresh(request: Request, admin_user_id: str = Query("")):
    _authenticated_admin_id(request, admin_user_id)
    await _invalidate_prefix("cache")
    return {"ok": True}


@app.get("/api/media/telegraph/{asset_key}/{asset_name}")
async def api_telegraph_media(asset_key: str, asset_name: str):
    try:
        asset_path = resolve_telegraph_asset_path(asset_key, asset_name)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    return FileResponse(
        asset_path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/")
async def root():
    return FileResponse(MINIAPP_DIR / "index.html")


@app.middleware("http")
async def add_perf_headers(request: Request, call_next):
    start = time.perf_counter()
    if request.url.path.startswith("/api/"):
        ip = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        ip = ip or (request.client.host if request.client else "unknown")
        bucket = int(time.time() // 60)
        key = f"{ip}:{bucket}"
        async with _RATE_LIMIT_LOCK:
            _, count = _RATE_LIMIT.get(key, (bucket, 0))
            count += 1
            _RATE_LIMIT[key] = (bucket, count)
            if len(_RATE_LIMIT) > 5000:
                current_bucket = bucket
                for old_key, (old_bucket, _) in list(_RATE_LIMIT.items()):
                    if old_bucket < current_bucket - 2:
                        _RATE_LIMIT.pop(old_key, None)
        if count > max(30, API_RATE_LIMIT_PER_MINUTE):
            return JSONResponse(
                {"detail": "Muitas requisicoes. Tente novamente em instantes."},
                status_code=429,
            )
    no_cache_index = request.url.path in {"/", "/miniapp", "/miniapp/", "/miniapp/index.html"}
    if no_cache_index:
        request.scope["headers"] = [
            (key, value)
            for key, value in request.scope.get("headers", [])
            if key.lower() not in {b"if-none-match", b"if-modified-since"}
        ]

    response: Response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Response-Time"] = f"{elapsed_ms}ms"
    if no_cache_index:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    else:
        response.headers["Cache-Control"] = response.headers.get("Cache-Control", "public, max-age=15")
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "path": str(request.url.path)},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Erro interno no miniapp.",
            "path": str(request.url.path),
            "error": str(exc),
        },
    )


if MINIAPP_DIR.exists():
    app.mount("/miniapp", StaticFiles(directory=MINIAPP_DIR, html=True), name="miniapp")
