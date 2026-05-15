import asyncio
import html
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import ADMIN_IDS, BOT_USERNAME, CANAL_POSTAGEM_MANGA, PREFERRED_CHAPTER_LANG, STICKER_DIVISOR
from core.channel_target import ensure_channel_target
from handlers.callbacks import _ALLOWED_TAG_TRANSLATIONS, _norm_tag_label
from services.catalog_client import (
    _request_form_json,
    flatten_chapters,
    get_cached_title_bundle,
    get_cached_title_overview,
    get_title_search,
    get_title_bundle,
    get_title_overview,
    search_titles,
)

STATUS_PT_MAP = {
    "ongoing": "Em andamento",
    "completed": "Finalizado",
    "hiatus": "Em hiato",
    "cancelled": "Cancelado",
    "dropped": "Cancelado",
    "releasing": "Em lançamento",
    "finished": "Finalizado",
}

FORMAT_PT_MAP = {
    "MANGA": "Mangá",
    "MANHWA": "Manhwa",
    "MANHUA": "Manhua",
    "ONE_SHOT": "One-shot",
    "NOVEL": "Novel",
}

BLOCKED_GENRE_EXACT = {
    "based on a korean novel",
    "based on a novel",
    "based on a web novel",
    "based on a light novel",
    "based on a webtoon",
    "based on a manhwa",
    "based on a manhua",
    "based on an anime",
    "based on a game",
    "based on a video game",
    "based on a movie",
    "based on a tv series",
    "adaptation",
}

BLOCKED_GENRE_PATTERNS = [
    r"^based on\b",
    r"\bnovel\b",
    r"\bweb novel\b",
    r"\bkorean novel\b",
    r"\blight novel\b",
    r"\badaptation\b",
]

CATALOG_SITE_BASE = os.getenv("CATALOG_SITE_BASE", "https://mangaball.net").rstrip("/")
POSTED_MANGAS_FILE = Path("data/mangas_postados.json")
POSTMANGA_POPULAR_LOG_FILE = Path("data/postmanga_popular_log.json")
BULK_DELAY_SECONDS = float(os.getenv("MANGA_BULK_DELAY_SECONDS", "30"))
POSTALL_DELAY_SECONDS = max(BULK_DELAY_SECONDS, float(os.getenv("POSTALLMANGAS_DELAY_SECONDS", "90")))
POSTALL_MAX_LIMIT = max(1, int(os.getenv("POSTALLMANGAS_MAX_LIMIT", "25")))
BULK_HTTP_TIMEOUT = 25.0
POPULAR_SCAN_LIMIT = int(os.getenv("POSTMANGA_POPULAR_SCAN_LIMIT", "1000"))
POSTALL_ADVANCED_PAGE_SIZE = int(os.getenv("POSTALL_ADVANCED_PAGE_SIZE", "24"))
DIVIDER_FALLBACK_TEXT = "━━━━━━━━━━━━━━"
BOTDATA_BULK_RUNNING_KEY = "postallmangas_running"
BOTDATA_BULK_TASK_KEY = "postallmangas_task"


def _truncate_text(text: str, limit: int = 320) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", (value or "").strip().lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(value.split())


def _pick_main_title(manga: dict) -> str:
    return (
        manga.get("display_title")
        or manga.get("title")
        or manga.get("preferred_title")
        or manga.get("name")
        or "Sem título"
    )


def _clean_description(description: str) -> str:
    description = (description or "").strip()
    description = re.sub(r"<br\s*/?>", "\n", description, flags=re.I)
    description = re.sub(r"</p\s*>", "\n", description, flags=re.I)
    description = re.sub(r"<[^>]+>", " ", description)
    description = html.unescape(description)
    return " ".join(description.split())


def _translate_status(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "N/A"
    return STATUS_PT_MAP.get(raw.lower(), raw)


def _translate_format(value: str) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return "Mangá"
    return FORMAT_PT_MAP.get(raw, raw.title())


def _format_rating_value(value: Any) -> str:
    text = str(value if value not in (None, "", []) else "").strip()
    if not text:
        return ""
    if "/" in text:
        return text
    try:
        number = float(text.replace(",", "."))
    except (TypeError, ValueError):
        return text
    if number <= 0:
        return ""
    if number > 10:
        number = number / 10
    return f"{number:.1f}"


def _display_rating(manga: dict) -> str:
    for value in (
        manga.get("rating"),
        manga.get("score"),
        manga.get("source_rating"),
        manga.get("site_rating"),
        manga.get("average_rating"),
        manga.get("anilist_score"),
    ):
        formatted = _format_rating_value(value)
        if formatted:
            return formatted
    return "0"


def _latest_chapter_summary(manga: dict) -> dict:
    latest = manga.get("latest_chapter")
    if isinstance(latest, dict) and latest.get("chapter_id"):
        return latest

    chapter_id = str(manga.get("chapter_id") or "").strip()
    if not chapter_id:
        return {}

    return {
        "chapter_id": chapter_id,
        "chapter_number": str(
            manga.get("latest_chapter")
            or manga.get("chapter_number")
            or manga.get("latest_chapter_number")
            or ""
        ).strip(),
    }


def _pick_best_candidate(query: str, results: list[dict]) -> dict | None:
    if not results:
        return None

    normalized_query = _normalize_text(query)

    def _score(item: dict) -> tuple[int, int, int]:
        display_title = _normalize_text(item.get("display_title") or "")
        title = _normalize_text(item.get("title") or "")
        raw_title = _normalize_text(item.get("raw_title") or "")
        best_text = display_title or title or raw_title

        if not best_text:
            return (-1, 0, 0)
        if best_text == normalized_query or title == normalized_query or raw_title == normalized_query:
            return (500, -len(best_text), 1 if item.get("chapter_id") else 0)
        if best_text.startswith(normalized_query) or title.startswith(normalized_query):
            return (400, -len(best_text), 1 if item.get("chapter_id") else 0)
        if normalized_query in best_text or normalized_query in raw_title:
            return (300, -len(best_text), 1 if item.get("chapter_id") else 0)

        overlap = len(set(normalized_query.split()) & set(best_text.split()))
        return (100 + overlap, -len(best_text), 1 if item.get("chapter_id") else 0)

    return max(results, key=_score)


def _flatten_strings(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, str):
        text = html.unescape(value).strip()
        if not text:
            return []
        parts = re.split(r"[|,/•]+|\s+(?=#)", text)
        return [p.strip() for p in parts if p.strip()]

    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_strings(item))
        return out

    if isinstance(value, dict):
        out: list[str] = []
        for key in ("name", "title", "label", "genre", "tag"):
            if value.get(key):
                out.extend(_flatten_strings(value.get(key)))
        return out

    return []


def _unique_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []

    for item in items:
        clean = " ".join(str(item).strip().split())
        if not clean:
            continue
        norm = _normalize_text(clean)
        if norm in seen:
            continue
        seen.add(norm)
        output.append(clean)

    return output


def _prettify_tag(value: str) -> str:
    value = html.unescape(str(value or "").strip())
    value = value.replace("_", " ")
    value = re.sub(r"\s+", " ", value).strip(" -#")
    return value


def _is_valid_display_genre(value: str) -> bool:
    raw = _prettify_tag(value)
    norm = _normalize_text(raw)

    if not norm:
        return False

    if norm in BLOCKED_GENRE_EXACT:
        return False

    for pattern in BLOCKED_GENRE_PATTERNS:
        if re.search(pattern, norm, flags=re.I):
            return False

    return True


def _filter_display_genres(items: list[str], limit: int = 6) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()

    for item in items or []:
        norm = _norm_tag_label(_prettify_tag(item))
        translated = _ALLOWED_TAG_TRANSLATIONS.get(norm)

        if not translated or norm in seen:
            continue

        seen.add(norm)
        output.append(translated[0])

        if len(output) >= limit:
            break

    return output


def _extract_json_ld_images(raw_html: str) -> list[str]:
    urls: list[str] = []
    if not raw_html:
        return urls

    matches = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        raw_html,
        flags=re.I | re.S,
    )

    for block in matches:
        block = block.strip()
        if not block:
            continue
        try:
            payload = json.loads(block)
        except Exception:
            continue

        def walk(obj: Any) -> None:
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key in {"thumbnailUrl", "contentUrl", "url", "image", "og:image"} and isinstance(value, str):
                        if value.startswith("http"):
                            urls.append(value)
                    else:
                        walk(value)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(payload)

    return urls


def _extract_meta_content(raw_html: str, prop: str) -> str:
    if not raw_html:
        return ""

    pattern = (
        r'<meta[^>]+(?:property|name)=["\']'
        + re.escape(prop)
        + r'["\'][^>]+content=["\']([^"\']+)["\']'
    )
    match = re.search(pattern, raw_html, flags=re.I)
    return html.unescape(match.group(1)).strip() if match else ""


def _extract_badges_from_html(raw_html: str) -> list[str]:
    if not raw_html:
        return []

    matches = re.findall(
        r'data-tag-id="[^"]+"[^>]*>([^<]+)</span>',
        raw_html,
        flags=re.I,
    )
    return _unique_keep_order([html.unescape(x).strip() for x in matches if x.strip()])


def _resolve_manga_genres(manga: dict) -> list[str]:
    candidates: list[str] = []

    for key in (
        "genres",
        "genre_names",
        "tags",
        "tag_names",
        "anilist_genres",
        "anilist_tags",
        "mangaball_genres",
        "mangaball_tags",
        "categories",
        "keywords",
    ):
        candidates.extend(_flatten_strings(manga.get(key)))

    raw_html = (
        manga.get("raw_html")
        or manga.get("html")
        or manga.get("page_html")
        or manga.get("title_html")
        or ""
    )
    candidates.extend(_extract_badges_from_html(raw_html))

    return _unique_keep_order(candidates)


def _resolve_origin_photo(manga: dict) -> str:
    raw_html = (
        manga.get("raw_html")
        or manga.get("html")
        or manga.get("page_html")
        or manga.get("title_html")
        or ""
    )

    candidates = [
        manga.get("origin_cover_url"),
        manga.get("site_cover_url"),
        manga.get("source_cover_url"),
        manga.get("og_image"),
        manga.get("seo_image"),
        manga.get("thumbnailUrl"),
        _extract_meta_content(raw_html, "og:image"),
        *_extract_json_ld_images(raw_html),
        manga.get("cover_url"),
        manga.get("banner_url"),
        manga.get("background_url"),
    ]

    for candidate in candidates:
        url = str(candidate or "").strip()
        if url.startswith("http"):
            return url
    return ""


def _resolve_description(manga: dict) -> str:
    raw_html = (
        manga.get("raw_html")
        or manga.get("html")
        or manga.get("page_html")
        or manga.get("title_html")
        or ""
    )

    description = (
        manga.get("description")
        or manga.get("synopsis")
        or manga.get("anilist_description")
        or manga.get("seo_description")
        or _extract_meta_content(raw_html, "description")
        or _extract_meta_content(raw_html, "og:description")
        or ""
    )

    return _clean_description(description)


def _resolve_year(manga: dict) -> str:
    for key in ("year", "release_year", "published_year", "start_year", "anilist_year"):
        value = str(manga.get(key) or "").strip()
        if value:
            return value

    raw_html = (
        manga.get("raw_html")
        or manga.get("html")
        or manga.get("page_html")
        or manga.get("title_html")
        or ""
    )
    match = re.search(r"Published:\s*<b>(\d{4})</b>", raw_html, flags=re.I)
    return match.group(1) if match else ""


def _merge_post_payload(overview: dict, search_item: dict, bundle: dict | None = None) -> dict:
    merged = dict(overview or {})
    if bundle:
        merged.update({key: value for key, value in bundle.items() if value not in (None, "", [], {})})

    if not merged.get("title_id"):
        merged["title_id"] = search_item.get("title_id") or ""
    if not merged.get("title"):
        merged["title"] = search_item.get("title") or ""
    if not merged.get("display_title"):
        merged["display_title"] = search_item.get("display_title") or merged.get("title") or ""

    if not merged.get("cover_url"):
        merged["cover_url"] = search_item.get("cover_url") or ""
    if not merged.get("background_url"):
        merged["background_url"] = search_item.get("background_url") or merged.get("cover_url") or ""
    if not merged.get("banner_url"):
        merged["banner_url"] = merged.get("background_url") or merged.get("cover_url") or ""

    origin_photo = _resolve_origin_photo(merged)
    if origin_photo:
        merged["origin_cover_url"] = origin_photo
        merged["cover_url"] = origin_photo
        if not merged.get("banner_url"):
            merged["banner_url"] = origin_photo

    genres = _resolve_manga_genres(merged)
    if genres:
        merged["genres"] = genres

    description = _resolve_description(merged)
    if description:
        merged["description"] = description

    year = _resolve_year(merged)
    if year:
        merged["year"] = year

    if not merged.get("latest_chapter"):
        latest = _latest_chapter_summary(search_item)
        if latest:
            merged["latest_chapter"] = latest

    return merged


def _build_caption(manga: dict) -> str:
    full_title = html.escape(_pick_main_title(manga))

    genres = _filter_display_genres(_resolve_manga_genres(manga), limit=6)
    genres_text = ", ".join(f"#{g.replace(' ', '_')}" for g in genres) if genres else "N/A"

    chapters = (
        manga.get("total_chapters")
        or manga.get("chapter_count")
        or manga.get("anilist_chapters")
        or "?"
    )
    status = _translate_status(manga.get("status") or manga.get("anilist_status") or "N/A")
    rating = _display_rating(manga)
    rating_text = str(rating) if "/" in str(rating) else f"{rating}/10"

    return (
        f"📚 <b>{full_title}</b>\n\n"
        f"<blockquote><b>Status:</b> <i>{html.escape(str(status))}</i>\n"
        f"<b>Capítulos:</b> <i>{html.escape(str(chapters))}</i>\n"
        f"<b>Nota:</b> <i>{html.escape(rating_text)}</i>\n"
        f"<b>Gêneros:</b> <i>{html.escape(genres_text)}</i></blockquote>\n\n"
        f"Mangás Brasil | @MangasBrasil"
    )



def _build_keyboard(manga: dict) -> InlineKeyboardMarkup:
    title_id = manga.get("title_id") or ""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📚 Ler obra", url=f"https://t.me/{BOT_USERNAME}?start=title_{title_id}")]]
    )


def _load_posted_manga_ids() -> list[str]:
    try:
        if not POSTED_MANGAS_FILE.exists():
            return []
        data = json.loads(POSTED_MANGAS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return []


def _save_posted_manga_ids(items: list[str]) -> None:
    POSTED_MANGAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    POSTED_MANGAS_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _post_photo_url(manga: dict) -> str:
    return (
        manga.get("origin_cover_url")
        or manga.get("cover_url")
        or manga.get("banner_url")
        or manga.get("background_url")
        or ""
    ).strip()


def _load_popular_post_log() -> dict:
    try:
        if not POSTMANGA_POPULAR_LOG_FILE.exists():
            return {"sent": {}, "skipped": {}}
        data = json.loads(POSTMANGA_POPULAR_LOG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"sent": {}, "skipped": {}}
        data.setdefault("sent", {})
        data.setdefault("skipped", {})
        return data
    except Exception:
        return {"sent": {}, "skipped": {}}


def _save_popular_post_log(data: dict) -> None:
    POSTMANGA_POPULAR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    POSTMANGA_POPULAR_LOG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _log_popular_result(log_data: dict, status: str, title_id: str, title_name: str, reason: str = "") -> None:
    title_id = str(title_id or "").strip()
    if not title_id:
        return
    bucket = "sent" if status == "sent" else "skipped"
    log_data.setdefault(bucket, {})
    log_data[bucket][title_id] = {
        "title": str(title_name or "Mangá").strip(),
        "reason": str(reason or "").strip(),
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    _save_popular_post_log(log_data)


def _popular_processed_ids(log_data: dict) -> set[str]:
    processed = set(_load_posted_manga_ids())
    processed.update(str(item).strip() for item in (log_data.get("sent") or {}).keys())
    processed.update(str(item).strip() for item in (log_data.get("skipped") or {}).keys())
    return {item for item in processed if item}


def _has_portuguese_chapter(bundle: dict | None, title_id: str) -> bool:
    if not bundle:
        return False
    chapters = flatten_chapters(
        {"title_id": title_id, "chapters": bundle.get("chapters") or []},
        PREFERRED_CHAPTER_LANG,
    )
    return bool(chapters)


async def _resolve_valid_popular_payload(ref: dict) -> tuple[dict | None, str]:
    title_id = str(ref.get("title_id") or "").strip()
    if not title_id:
        return None, "sem_id"

    overview = get_cached_title_overview(title_id)
    if overview is None:
        try:
            overview = await get_title_overview(title_id)
        except Exception:
            overview = {}

    bundle = get_cached_title_bundle(title_id)
    if bundle is None:
        try:
            bundle = await asyncio.wait_for(get_title_bundle(title_id), timeout=12.0)
        except Exception:
            bundle = None

    if not _has_portuguese_chapter(bundle, title_id):
        return None, "sem_capitulo_pt_br"

    payload = _merge_post_payload(overview or {}, ref, bundle)
    if not _post_photo_url(payload):
        return None, "sem_imagem"

    return payload, ""


def _bulk_running(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.application.bot_data.get(BOTDATA_BULK_RUNNING_KEY, False))


def _set_bulk_running(context: ContextTypes.DEFAULT_TYPE, value: bool) -> None:
    context.application.bot_data[BOTDATA_BULK_RUNNING_KEY] = value


async def _safe_edit_status(message, text: str) -> None:
    try:
        await message.edit_text(text, parse_mode="HTML")
    except Exception:
        pass


async def _send_divider(bot, destination) -> None:
    sticker = str(STICKER_DIVISOR or "").strip()
    sticker_error = None

    if sticker:
        for _ in range(3):
            try:
                await bot.send_sticker(chat_id=destination, sticker=sticker)
                return
            except Exception as error:
                sticker_error = error
                await asyncio.sleep(0.8)

        print("ERRO STICKER DIVISOR MANGÁ:", repr(sticker_error), sticker)

    try:
        await bot.send_message(chat_id=destination, text=DIVIDER_FALLBACK_TEXT)
    except Exception as fallback_error:
        print("ERRO DIVISOR FALLBACK MANGÁ:", repr(fallback_error))
        if sticker_error:
            raise sticker_error
        raise


async def _send_manga_post(bot, destination, manga: dict, *, require_photo: bool = False) -> None:
    photo = _post_photo_url(manga) or None

    caption = _build_caption(manga)
    keyboard = _build_keyboard(manga)

    if photo:
        try:
            await bot.send_photo(
                chat_id=destination,
                photo=photo,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as photo_error:
            print("ERRO POSTMANGA FOTO:", repr(photo_error))
            if require_photo:
                raise RuntimeError("imagem_nao_enviada") from photo_error
            await bot.send_message(
                chat_id=destination,
                text=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
    else:
        if require_photo:
            raise RuntimeError("sem_imagem")
        await bot.send_message(
            chat_id=destination,
            text=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    await _send_divider(bot, destination)


def _extract_xml_locs(xml_text: str) -> list[str]:
    if not xml_text:
        return []
    return [
        html.unescape(x.strip())
        for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml_text, flags=re.I | re.S)
        if x.strip()
    ]


def _title_from_slug(slug: str) -> str:
    text = slug.replace("-", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text.title() if text else "Mangá"


def _title_ref_from_url(url: str) -> dict | None:
    clean = (url or "").strip()

    match = re.search(
        r"/title-detail/(?P<slug>.+)-(?P<title_id>[a-zA-Z0-9]{12,})/?$",
        clean,
        flags=re.I,
    )
    if not match:
        return None

    title_id = match.group("title_id").strip()
    slug = match.group("slug").strip()
    guessed_title = _title_from_slug(slug)

    return {
        "title_id": title_id,
        "title": guessed_title,
        "display_title": guessed_title,
        "raw_title": guessed_title,
        "url": clean,
    }


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()
    return response.text


async def _load_pending_title_refs(limit: int | None = None) -> list[dict]:
    posted = set(_load_posted_manga_ids())
    pending: list[dict] = []
    seen: set[str] = set()

    index_url = f"{CATALOG_SITE_BASE}/storage/sitemaps/sitemap-title-index.xml"

    async with httpx.AsyncClient(timeout=BULK_HTTP_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}) as client:
        index_xml = await _fetch_text(client, index_url)
        sitemap_urls = [u for u in _extract_xml_locs(index_xml) if u.lower().endswith(".xml")]

        if not sitemap_urls:
            sitemap_urls = [index_url]

        for sitemap_url in sitemap_urls:
            sitemap_xml = await _fetch_text(client, sitemap_url)
            title_urls = [u for u in _extract_xml_locs(sitemap_xml) if "/title-detail/" in u]

            for title_url in title_urls:
                ref = _title_ref_from_url(title_url)
                if not ref:
                    continue

                title_id = ref["title_id"]
                if title_id in seen or title_id in posted:
                    continue

                seen.add(title_id)
                pending.append(ref)

                if limit is not None and len(pending) >= limit:
                    return pending

    return pending


def _advanced_pt_br_filter_payload(page: int) -> dict:
    return {
        "search_input": "",
        "filters[sort]": "updated_chapters_desc",
        "filters[page]": str(max(1, int(page))),
        "filters[tag_included_ids]": "",
        "filters[tag_included_mode]": "and",
        "filters[tag_excluded_ids]": "",
        "filters[tag_excluded_mode]": "and",
        "filters[contentRating]": "any",
        "filters[demographic]": "any",
        "filters[person]": "any",
        "filters[originalLanguages]": "any",
        "filters[publicationYear]": "",
        "filters[publicationStatus]": "any",
        "filters[translatedLanguage][]": PREFERRED_CHAPTER_LANG or "pt-br",
        "filters[userSettingsEnabled]": "false",
    }


def _title_ref_from_advanced_item(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None

    title_id = str(item.get("_id") or item.get("title_id") or item.get("id") or "").strip()
    url = str(item.get("url") or "").strip()
    if not title_id:
        ref = _title_ref_from_url(url)
        if not ref:
            return None
        title_id = ref["title_id"]

    title = str(item.get("name") or item.get("title") or item.get("display_title") or "").strip()
    if not title:
        title = (_title_ref_from_url(url) or {}).get("title") or "Mangá"

    return {
        "title_id": title_id,
        "title": title,
        "display_title": title,
        "raw_title": title,
        "url": url,
        "cover_url": item.get("cover") or item.get("img") or item.get("image") or "",
        "background_url": item.get("background") or item.get("cover") or "",
        "status": _clean_description(str(item.get("status") or "")),
        "latest_chapter": item.get("last_chapter") or item.get("latest_chapter") or "",
        "adult": bool(item.get("isAdult") or item.get("adult") or item.get("is_adult")),
    }


async def _load_pending_title_refs_from_pt_filter(limit: int | None = None) -> list[dict]:
    posted = set(_load_posted_manga_ids())
    pending: list[dict] = []
    seen: set[str] = set()
    page = 1
    last_page = 1

    while True:
        response = await _request_form_json(
            "/api/v1/title/search-advanced/",
            _advanced_pt_br_filter_payload(page),
        )
        if response.get("code") != 200:
            break

        for item in response.get("data") or []:
            ref = _title_ref_from_advanced_item(item)
            if not ref:
                continue

            title_id = ref["title_id"]
            if title_id in seen or title_id in posted:
                continue

            seen.add(title_id)
            pending.append(ref)

            if limit is not None and len(pending) >= limit:
                return pending

        pagination = response.get("pagination") or {}
        last_page = int(pagination.get("last_page") or last_page or 1)
        if page >= last_page:
            break
        if not response.get("data"):
            break
        page += 1

    return pending


async def _resolve_payload_from_ref(ref: dict) -> dict | None:
    title_id = str(ref.get("title_id") or "").strip()
    if not title_id:
        return None

    overview = get_cached_title_overview(title_id)
    if overview is None:
        try:
            overview = await get_title_overview(title_id)
        except Exception:
            overview = {}

    bundle = get_cached_title_bundle(title_id)
    if bundle is None:
        try:
            bundle = await asyncio.wait_for(get_title_bundle(title_id), timeout=12.0)
        except Exception:
            bundle = None

    return _merge_post_payload(overview or {}, ref, bundle)


async def _resolve_valid_postall_payload(ref: dict) -> tuple[dict | None, str]:
    title_id = str(ref.get("title_id") or "").strip()
    if not title_id:
        return None, "sem_id"

    payload = _merge_post_payload({}, ref, None)
    latest = _latest_chapter_summary(payload)
    if latest:
        payload["latest_chapter"] = latest
        payload.setdefault("chapter_count", 1)

    if not _post_photo_url(payload):
        return None, "sem_imagem"

    return payload, ""


def _reason_label(reason: str) -> str:
    labels = {
        "sem_id": "sem ID",
        "sem_capitulo_pt_br": "sem capítulo em português",
        "sem_imagem": "sem imagem",
    }
    return labels.get(str(reason or "").strip(), str(reason or "ignorado"))


async def _run_postmanga_popular(
    context: ContextTypes.DEFAULT_TYPE,
    admin_chat_id: int,
    reply_to_message_id: int | None,
    target_count: int,
) -> None:
    _set_bulk_running(context, True)
    status_message = None

    try:
        destination = await ensure_channel_target(context.bot, CANAL_POSTAGEM_MANGA or admin_chat_id)
        log_data = _load_popular_post_log()
        processed_ids = _popular_processed_ids(log_data)
        posted_ids = _load_posted_manga_ids()
        posted_set = set(posted_ids)

        scan_limit = max(30, target_count * 3)
        max_scan = max(scan_limit, POPULAR_SCAN_LIMIT, target_count * 10)
        sent = 0
        skipped = 0
        failed = 0
        scanned = 0
        last_title = "-"
        last_skip = "-"

        status_message = await context.bot.send_message(
            chat_id=admin_chat_id,
            text=(
                "🚀 <b>Postagem popular iniciada.</b>\n\n"
                f"<b>Meta:</b> <code>{target_count}</code>\n"
                "<b>Fonte:</b> <code>mais populares</code>\n"
                "<i>Vou pular obras sem imagem ou sem capítulo em português.</i>"
            ),
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id,
        )

        while sent < target_count and scan_limit <= max_scan:
            refs = await get_title_search("getPopular", limit=scan_limit)
            candidates = [
                ref
                for ref in refs
                if str(ref.get("title_id") or "").strip()
                and str(ref.get("title_id") or "").strip() not in processed_ids
            ]

            if not candidates:
                if scan_limit >= max_scan:
                    break
                scan_limit = min(max_scan, max(scan_limit + target_count, scan_limit * 2))
                continue

            for ref in candidates:
                if sent >= target_count:
                    break

                title_id = str(ref.get("title_id") or "").strip()
                title_name = str(ref.get("display_title") or ref.get("title") or "Mangá").strip()
                processed_ids.add(title_id)
                scanned += 1
                last_title = title_name

                try:
                    payload, reason = await _resolve_valid_popular_payload(ref)
                    if not payload:
                        skipped += 1
                        last_skip = f"{title_name} ({_reason_label(reason)})"
                        _log_popular_result(log_data, "skipped", title_id, title_name, reason)
                    else:
                        await _send_manga_post(context.bot, destination, payload, require_photo=True)
                        sent += 1
                        if title_id and title_id not in posted_set:
                            posted_ids.append(title_id)
                            posted_set.add(title_id)
                            _save_posted_manga_ids(posted_ids)
                        _log_popular_result(log_data, "sent", title_id, title_name, "")

                        if sent < target_count:
                            await asyncio.sleep(BULK_DELAY_SECONDS)
                except Exception as error:
                    failed += 1
                    reason = f"erro: {str(error)[:160]}"
                    last_skip = f"{title_name} ({reason})"
                    _log_popular_result(log_data, "skipped", title_id, title_name, reason)
                    print("ERRO POSTMANGA POPULAR:", repr(error), title_id, title_name)

                await _safe_edit_status(
                    status_message,
                    (
                        "🚀 <b>Postagem popular em andamento.</b>\n\n"
                        f"<b>Meta:</b> <code>{target_count}</code>\n"
                        f"<b>Enviados:</b> <code>{sent}</code>\n"
                        f"<b>Pulados:</b> <code>{skipped}</code>\n"
                        f"<b>Falhas:</b> <code>{failed}</code>\n"
                        f"<b>Verificados:</b> <code>{scanned}</code>\n"
                        f"<b>Busca atual:</b> <code>top {scan_limit}</code>\n"
                        f"<b>Atual:</b> <code>{html.escape(last_title)}</code>\n"
                        f"<b>Último pulo:</b> <code>{html.escape(last_skip)}</code>"
                    ),
                )

            if sent >= target_count:
                break

            if scan_limit >= max_scan:
                break
            scan_limit = min(max_scan, max(scan_limit + target_count, scan_limit * 2))

        final_title = "✅ <b>Meta concluída.</b>" if sent >= target_count else "⚠️ <b>Postagem popular finalizada.</b>"
        await _safe_edit_status(
            status_message,
            (
                f"{final_title}\n\n"
                f"<b>Meta:</b> <code>{target_count}</code>\n"
                f"<b>Enviados:</b> <code>{sent}</code>\n"
                f"<b>Pulados:</b> <code>{skipped}</code>\n"
                f"<b>Falhas:</b> <code>{failed}</code>\n"
                f"<b>Verificados:</b> <code>{scanned}</code>\n"
                f"<b>Limite de busca:</b> <code>top {scan_limit}</code>"
            ),
        )

    finally:
        _set_bulk_running(context, False)
        context.application.bot_data.pop(BOTDATA_BULK_TASK_KEY, None)


async def _run_postallmangas(
    context: ContextTypes.DEFAULT_TYPE,
    admin_chat_id: int,
    reply_to_message_id: int | None,
    limit: int | None,
) -> None:
    _set_bulk_running(context, True)
    try:
        destination = await ensure_channel_target(context.bot, CANAL_POSTAGEM_MANGA or admin_chat_id)
        refs = await _load_pending_title_refs_from_pt_filter(limit=limit)

        if not refs:
            await context.bot.send_message(
                chat_id=admin_chat_id,
                text="✅ <b>Não há mangás pendentes em português para postar.</b>",
                parse_mode="HTML",
                reply_to_message_id=reply_to_message_id,
            )
            return

        status_message = await context.bot.send_message(
            chat_id=admin_chat_id,
            text=(
                "🚀 <b>Postagem em lote iniciada.</b>\n\n"
                f"<b>Fonte:</b> <code>Search Advanced / {html.escape(PREFERRED_CHAPTER_LANG or 'pt-br')}</code>\n"
                f"<b>Pendentes encontrados:</b> <code>{len(refs)}</code>\n"
                f"<b>Intervalo:</b> <code>{int(POSTALL_DELAY_SECONDS)}s</code>"
            ),
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id,
        )

        posted_ids = _load_posted_manga_ids()
        posted_set = set(posted_ids)

        sent = 0
        skipped = 0
        failed = 0
        total = len(refs)
        last_skip = "-"

        for index, ref in enumerate(refs, start=1):
            title_id = str(ref.get("title_id") or "").strip()
            title_name = str(ref.get("display_title") or ref.get("title") or "Mangá").strip()

            try:
                payload, reason = await _resolve_valid_postall_payload(ref)
                if not payload:
                    skipped += 1
                    last_skip = f"{title_name} ({_reason_label(reason)})"
                    continue

                await _send_manga_post(context.bot, destination, payload)

                if title_id and title_id not in posted_set:
                    posted_ids.append(title_id)
                    posted_set.add(title_id)
                    _save_posted_manga_ids(posted_ids)

                sent += 1
            except Exception as error:
                failed += 1
                print("ERRO POSTALLMANGAS:", repr(error), title_id, title_name)

            await _safe_edit_status(
                status_message,
                (
                    "🚀 <b>Postagem em lote em andamento.</b>\n\n"
                    f"<b>Enviados:</b> <code>{sent}</code>\n"
                    f"<b>Pulados:</b> <code>{skipped}</code>\n"
                    f"<b>Falhas:</b> <code>{failed}</code>\n"
                    f"<b>Processados:</b> <code>{index}/{total}</code>\n"
                    f"<b>Atual:</b> <code>{html.escape(title_name)}</code>\n"
                    f"<b>Último pulo:</b> <code>{html.escape(last_skip)}</code>"
                ),
            )

            if index < total:
                await asyncio.sleep(POSTALL_DELAY_SECONDS)

        await _safe_edit_status(
            status_message,
            (
                "✅ <b>Postagem em lote finalizada.</b>\n\n"
                f"<b>Enviados:</b> <code>{sent}</code>\n"
                f"<b>Pulados:</b> <code>{skipped}</code>\n"
                f"<b>Falhas:</b> <code>{failed}</code>\n"
                f"<b>Total:</b> <code>{total}</code>"
            ),
        )

    finally:
        _set_bulk_running(context, False)
        context.application.bot_data.pop(BOTDATA_BULK_TASK_KEY, None)


async def postmanga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    message = update.effective_message

    if not message:
        return

    if not _is_admin(user_id):
        await message.reply_text(
            "❌ <b>Você não tem permissão para usar este comando.</b>",
            parse_mode="HTML",
        )
        return

    if not context.args:
        await message.reply_text(
            "❌ <b>Faltou o nome do mangá.</b>\n\n"
            "Use assim:\n"
            "<code>/postmanga nome do mangá</code>\n\n"
            "Ou poste populares em lote:\n"
            "<code>/postmanga 10</code>\n\n"
            "📌 <b>Exemplo:</b>\n"
            "<code>/postmanga solo leveling</code>",
            parse_mode="HTML",
        )
        return

    if len(context.args) == 1 and str(context.args[0]).strip().isdigit():
        target_count = int(str(context.args[0]).strip())
        if target_count <= 0:
            await message.reply_text(
                "❌ <b>A quantidade precisa ser maior que zero.</b>",
                parse_mode="HTML",
            )
            return

        if _bulk_running(context):
            await message.reply_text(
                "⏳ <b>Já existe uma postagem em lote rodando agora.</b>",
                parse_mode="HTML",
            )
            return

        task = context.application.create_task(
            _run_postmanga_popular(
                context=context,
                admin_chat_id=message.chat_id,
                reply_to_message_id=message.message_id,
                target_count=target_count,
            )
        )
        context.application.bot_data[BOTDATA_BULK_TASK_KEY] = task
        await message.reply_text(
            (
                "🚀 <b>Fila de populares iniciada.</b>\n\n"
                f"Vou postar <code>{target_count}</code> obras populares válidas.\n"
                "<i>Sem imagem ou sem capítulo em português = pulo e registro.</i>"
            ),
            parse_mode="HTML",
        )
        return

    query = " ".join(context.args).strip()
    status_message = await message.reply_text(
        "📤 <b>Montando postagem...</b>\nAguarde um instante.",
        parse_mode="HTML",
    )

    try:
        results = await search_titles(query, limit=8)
        if not results:
            await status_message.edit_text("❌ <b>Não encontrei esse mangá.</b>", parse_mode="HTML")
            return

        search_item = _pick_best_candidate(query, results)
        if not search_item or not search_item.get("title_id"):
            await status_message.edit_text("❌ <b>Não consegui identificar a obra certa.</b>", parse_mode="HTML")
            return

        await status_message.edit_text(
            "📤 <b>Montando postagem...</b>\nResolvi a obra e estou preparando o card do canal.",
            parse_mode="HTML",
        )

        title_id = search_item["title_id"]

        overview = get_cached_title_overview(title_id)
        if overview is None:
            try:
                overview = await get_title_overview(title_id)
            except Exception:
                overview = {}

        bundle = get_cached_title_bundle(title_id)
        if bundle is None:
            try:
                bundle = await asyncio.wait_for(get_title_bundle(title_id), timeout=12.0)
            except Exception:
                bundle = None

        manga = _merge_post_payload(overview, search_item, bundle)
        destination = await ensure_channel_target(context.bot, CANAL_POSTAGEM_MANGA or message.chat_id)

        await _send_manga_post(context.bot, destination, manga)

        await status_message.edit_text(
            f"✅ <b>Postagem enviada com sucesso.</b>\n\n<code>{html.escape(manga.get('title') or query)}</code>",
            parse_mode="HTML",
        )
    except Exception as error:
        print("ERRO POSTMANGA:", repr(error))
        await status_message.edit_text(
            f"❌ <b>Não consegui postar esse mangá.</b>\n\n{html.escape(str(error) or 'Tente novamente em instantes.')}",
            parse_mode="HTML",
        )


async def postallmangas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    message = update.effective_message

    if not message:
        return

    if not _is_admin(user_id):
        await message.reply_text(
            "<b>Voce nao tem permissao para usar este comando.</b>",
            parse_mode="HTML",
        )
        return

    if _bulk_running(context):
        await message.reply_text(
            "<b>Ja existe uma postagem em lote rodando agora.</b>",
            parse_mode="HTML",
        )
        return

    if not context.args:
        await message.reply_text(
            "<b>Postagem em massa protegida.</b>\n\n"
            "Para nao derrubar a sessao do Mangaball, esse comando agora exige quantidade.\n\n"
            "Use: <code>/postallmangas 5</code>\n"
            f"Limite por rodada: <code>{POSTALL_MAX_LIMIT}</code>\n"
            f"Intervalo entre posts: <code>{int(POSTALL_DELAY_SECONDS)}s</code>",
            parse_mode="HTML",
        )
        return

    raw = str(context.args[0]).strip()
    if not raw.isdigit():
        await message.reply_text(
            "<b>Quantidade invalida.</b>\n\n"
            "Use: <code>/postallmangas 5</code>",
            parse_mode="HTML",
        )
        return

    limit = int(raw)
    if limit <= 0:
        await message.reply_text(
            "<b>A quantidade precisa ser maior que zero.</b>",
            parse_mode="HTML",
        )
        return
    if limit > POSTALL_MAX_LIMIT:
        await message.reply_text(
            "<b>Quantidade alta demais.</b>\n\n"
            f"Para proteger o bot e o cookie do Cloudflare, o maximo por rodada e <code>{POSTALL_MAX_LIMIT}</code>.",
            parse_mode="HTML",
        )
        return

    task = context.application.create_task(
        _run_postallmangas(
            context=context,
            admin_chat_id=message.chat_id,
            reply_to_message_id=message.message_id,
            limit=limit,
        )
    )
    context.application.bot_data[BOTDATA_BULK_TASK_KEY] = task

    await message.reply_text(
        "<b>Fila de postagem protegida iniciada.</b>\n\n"
        f"Vou postar ate <code>{limit}</code> mangas pendentes.\n"
        f"Intervalo entre posts: <code>{int(POSTALL_DELAY_SECONDS)}s</code>.",
        parse_mode="HTML",
    )
