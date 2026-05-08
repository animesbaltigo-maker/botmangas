import asyncio
import html
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from config import BALTIGO_UNIVERSE_WEBAPP_URL, BOT_BRAND, BOT_USERNAME, WEBAPP_BASE_URL
from core.background import fire_and_forget_sync, run_sync
from handlers.callbacks import send_chapter_panel, send_chapters_page, send_title_panel
from services.catalog_client import get_cached_home_snapshot, schedule_warm_catalog_cache
from services.i18n import t_user
from services.metrics import mark_user_seen
from services.referral_db import (
    create_referral,
    register_interaction,
    register_referral_click,
    upsert_user,
)
from services.user_registry import register_user
from utils.gatekeeper import ensure_channel_membership

START_COOLDOWN = 1.0
START_DEEP_LINK_TTL = 8.0
START_OPEN_TIMEOUT = 28.0
START_BANNER_URL = "https://photo.chelpbot.me/AgACAgEAAxkBa-uhxmn-Ynl49i4sc-QGdecUy6MivtqbAAJlDGsbr6GIR8U7RAEQqdGuAQADAgADeQADOwQ/photo.jpg"

_START_USER_LOCKS: dict[int, asyncio.Lock] = {}
_START_INFLIGHT: dict[str, float] = {}


def _extract_title_id(arg: str) -> str:
    return arg[6:] if arg.startswith("title_") else ""


def _extract_chapters_title_id(arg: str) -> str:
    return arg[9:] if arg.startswith("chapters_") else ""


def _extract_chapter_id(arg: str) -> str:
    for prefix in ("ch_", "cap_", "read_"):
        if arg.startswith(prefix):
            raw = arg[len(prefix):]
            return raw.split("_", 1)[0].strip() if "_" in raw else raw.strip()
    return ""


def _safe_user_lock(user_id: int) -> asyncio.Lock:
    lock = _START_USER_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _START_USER_LOCKS[user_id] = lock
    return lock


def _now() -> float:
    return time.monotonic()


def _deep_link_key(user_id: int, payload: str) -> str:
    return f"{user_id}:{payload}"


def _is_inflight(user_id: int, payload: str) -> bool:
    key = _deep_link_key(user_id, payload)
    item = _START_INFLIGHT.get(key)
    if not item:
        return False
    if _now() - item > START_DEEP_LINK_TTL:
        _START_INFLIGHT.pop(key, None)
        return False
    return True


def _set_inflight(user_id: int, payload: str) -> None:
    _START_INFLIGHT[_deep_link_key(user_id, payload)] = _now()


def _clear_inflight(user_id: int, payload: str) -> None:
    _START_INFLIGHT.pop(_deep_link_key(user_id, payload), None)


def _start_last_key(user_id: int) -> str:
    return f"start_last:{user_id}"


def _start_last_payload_key(user_id: int) -> str:
    return f"start_last_payload:{user_id}"


def _is_start_cooldown(context: ContextTypes.DEFAULT_TYPE, user_id: int, payload: str) -> bool:
    now = _now()
    last_ts = context.user_data.get(_start_last_key(user_id), 0.0)
    last_payload = context.user_data.get(_start_last_payload_key(user_id), "")
    if payload and payload == last_payload and (now - last_ts) < START_COOLDOWN:
        return True
    context.user_data[_start_last_key(user_id)] = now
    context.user_data[_start_last_payload_key(user_id)] = payload
    return False


def _queue_user_touch(user) -> None:
    username = user.username or ""
    first_name = user.first_name or ""

    def _runner():
        upsert_user(user.id, username, first_name)
        register_user(user.id)
        register_interaction(user.id)
        mark_user_seen(user.id, user.username or user.first_name or "")

    fire_and_forget_sync(_runner)


async def _safe_delete_message(message) -> None:
    if not message:
        return
    try:
        await message.delete()
    except TelegramError:
        pass
    except Exception:
        pass


async def _safe_edit_message(message, text: str) -> None:
    if not message:
        return
    try:
        await message.edit_text(text, parse_mode="HTML")
    except TelegramError:
        pass
    except Exception:
        pass


async def _send_start_loading(message, user_id: int, *, kind: str):
    key = "start.loading_chapter" if kind == "chapter" else "start.loading_title"
    try:
        return await message.reply_text(t_user(user_id, key), parse_mode="HTML")
    except Exception:
        return None


async def _handle_referral(arg: str, user, message) -> None:
    try:
        referrer_id = int(arg.split("_", 1)[1])
    except Exception:
        return

    await run_sync(register_referral_click, referrer_id, user.id)
    ok, reason = await run_sync(create_referral, referrer_id, user.id)
    if ok:
        text = t_user(user.id, "start.ref_ok")
    elif reason == "self":
        text = t_user(user.id, "start.ref_self")
    elif reason == "already_same":
        text = t_user(user.id, "start.ref_already_same")
    elif reason == "exists":
        text = t_user(user.id, "start.ref_exists")
    else:
        text = t_user(user.id, "common.generic_error")
    await message.reply_text(text, parse_mode="HTML")


def _miniapp_title_url(title_id: str, user_id: int | None = None) -> str:
    base = (WEBAPP_BASE_URL or "").rstrip("/")
    if not base:
        return ""
    suffix = f"&user_id={int(user_id)}" if user_id else ""
    return f"{base}/miniapp/index.html?title_id={title_id}{suffix}"


def build_welcome_panel(user_id: int, first_name: str) -> tuple[str, InlineKeyboardMarkup]:
    snapshot = get_cached_home_snapshot(limit=4)
    featured = snapshot.get("featured") or []

    keyboard_rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(t_user(user_id, "common.search"), switch_inline_query_current_chat="")],
        [
            InlineKeyboardButton(t_user(user_id, "common.language"), callback_data="mb|uilangmenu"),
            InlineKeyboardButton(t_user(user_id, "common.plans"), callback_data="mb|plan"),
        ],
    ]

    if BOT_USERNAME:
        keyboard_rows.append([InlineKeyboardButton(t_user(user_id, "common.referrals"), callback_data="noop_indicar")])

    for item in featured[:4]:
        title_id = str(item.get("title_id") or "").strip()
        url = _miniapp_title_url(title_id, user_id)
        if title_id and url:
            keyboard_rows.append(
                [[InlineKeyboardButton(f"📘 {item.get('title') or 'Mangá'}", web_app=WebAppInfo(url=url))]]
            )

    keyboard_rows.append(
        [InlineKeyboardButton("⚔️ Universo Baltigo", web_app=WebAppInfo(url=BALTIGO_UNIVERSE_WEBAPP_URL))]
    )

    text = t_user(
        user_id,
        "start.welcome",
        brand=html.escape(BOT_BRAND or "Mangas Baltigo"),
        name=html.escape(first_name or "leitor"),
    )
    return text, InlineKeyboardMarkup(keyboard_rows)


async def _send_welcome(message, user_id: int, first_name: str) -> None:
    snapshot = get_cached_home_snapshot(limit=4)
    featured = snapshot.get("featured") or []
    schedule_warm_catalog_cache()

    keyboard_rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(t_user(user_id, "common.search"), switch_inline_query_current_chat="")],
        [
            InlineKeyboardButton(t_user(user_id, "common.language"), callback_data="mb|uilangmenu"),
            InlineKeyboardButton(t_user(user_id, "common.plans"), callback_data="mb|plan"),
        ],
    ]

    if BOT_USERNAME:
        keyboard_rows.append([InlineKeyboardButton(t_user(user_id, "common.referrals"), callback_data="noop_indicar")])

    for item in featured[:4]:
        title_id = str(item.get("title_id") or "").strip()
        url = _miniapp_title_url(title_id, user_id)
        if not title_id or not url:
            continue
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    f"📘 {item.get('title') or 'Mangá'}",
                    web_app=WebAppInfo(url=url),
                )
            ]
        )

    keyboard_rows.append(
        [InlineKeyboardButton("⚔️ Universo Baltigo", web_app=WebAppInfo(url=BALTIGO_UNIVERSE_WEBAPP_URL))]
    )

    text = t_user(
        user_id,
        "start.welcome",
        brand=html.escape(BOT_BRAND or "Mangas Baltigo"),
        name=html.escape(first_name or "leitor"),
    )

    try:
        await message.reply_photo(
            photo=START_BANNER_URL,
            caption=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
        )
    except Exception:
            await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
            disable_web_page_preview=True,
        )


async def edit_home_panel(query, user_id: int, first_name: str = "leitor") -> None:
    schedule_warm_catalog_cache()
    text, markup = build_welcome_panel(user_id, first_name)
    try:
        if getattr(query.message, "photo", None):
            await query.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=markup)
        else:
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        return
    except BadRequest as error:
        if "message is not modified" in str(error).lower():
            return
    except TelegramError:
        pass

    if query.message:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    _queue_user_touch(user)
    if not await ensure_channel_membership(update, context):
        return

    arg = (context.args[0].strip() if context.args else "")
    if arg.startswith("ref_"):
        await _handle_referral(arg, user, message)
        arg = ""

    if arg and _is_start_cooldown(context, user.id, arg):
        await message.reply_text(t_user(user.id, "start.cooldown"), parse_mode="HTML")
        return
    if arg and _is_inflight(user.id, arg):
        await message.reply_text(t_user(user.id, "start.duplicate"), parse_mode="HTML")
        return

    user_lock = _safe_user_lock(user.id)
    async with user_lock:
        if arg and _is_inflight(user.id, arg):
            await message.reply_text(t_user(user.id, "start.duplicate"), parse_mode="HTML")
            return
        if arg:
            _set_inflight(user.id, arg)

        try:
            title_id = _extract_title_id(arg)
            if title_id:
                loading_msg = await _send_start_loading(message, user.id, kind="title")
                try:
                    await asyncio.wait_for(
                        send_title_panel(message, context, title_id, user.id, edit=False),
                        timeout=START_OPEN_TIMEOUT,
                    )
                    await _safe_delete_message(loading_msg)
                except asyncio.TimeoutError:
                    await _safe_edit_message(loading_msg, t_user(user.id, "start.timeout"))
                except Exception as error:
                    print("ERRO START OBRA:", repr(error))
                    await _safe_edit_message(loading_msg, t_user(user.id, "common.generic_error"))
                return

            chapters_title_id = _extract_chapters_title_id(arg)
            if chapters_title_id:
                loading_msg = await _send_start_loading(message, user.id, kind="chapters")
                try:
                    await asyncio.wait_for(
                        send_chapters_page(message, context, chapters_title_id, 1, user.id, edit=False),
                        timeout=START_OPEN_TIMEOUT,
                    )
                    await _safe_delete_message(loading_msg)
                except asyncio.TimeoutError:
                    await _safe_edit_message(loading_msg, t_user(user.id, "start.timeout"))
                except Exception as error:
                    print("ERRO START LISTA CAPITULOS:", repr(error))
                    await _safe_edit_message(loading_msg, t_user(user.id, "common.generic_error"))
                return

            chapter_id = _extract_chapter_id(arg)
            if chapter_id:
                loading_msg = await _send_start_loading(message, user.id, kind="chapter")
                try:
                    await asyncio.wait_for(
                        send_chapter_panel(message, context, chapter_id, user.id, edit=False),
                        timeout=START_OPEN_TIMEOUT,
                    )
                    await _safe_delete_message(loading_msg)
                except asyncio.TimeoutError:
                    await _safe_edit_message(loading_msg, t_user(user.id, "start.timeout"))
                except Exception as error:
                    print("ERRO START CAPITULO:", repr(error))
                    await _safe_edit_message(loading_msg, t_user(user.id, "common.generic_error"))
                return

            await _send_welcome(message, user.id, user.first_name or "leitor")
        finally:
            if arg:
                _clear_inflight(user.id, arg)
            await _safe_delete_message(message)
