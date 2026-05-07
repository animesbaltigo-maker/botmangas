from __future__ import annotations

import html
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import BOT_BRAND, WEBAPP_BASE_URL
from core.background import run_sync
from services.profile_stats import get_webapp_profile_stats
from utils.gatekeeper import ensure_channel_membership
from utils.profile_card import build_profile_card


async def _download_user_avatar(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bytes | None:
    try:
        photos = await context.bot.get_user_profile_photos(user_id=user_id, limit=1)
        if not photos.total_count or not photos.photos:
            return None
        photo = photos.photos[0][-1]
        telegram_file = await context.bot.get_file(photo.file_id)
        buffer = BytesIO()
        await telegram_file.download_to_memory(out=buffer)
        return buffer.getvalue()
    except TelegramError:
        return None
    except Exception:
        return None


def _miniapp_url(route: str, user_id: int | None = None) -> str:
    base = (WEBAPP_BASE_URL or "").strip().rstrip("/")
    if not base:
        return ""
    suffix = f"&user_id={int(user_id)}" if user_id else ""
    return f"{base}/miniapp/index.html?route={route}&page={route}&view={route}{suffix}"


def _profile_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup | None:
    history_url = _miniapp_url("history", user_id)
    favorites_url = _miniapp_url("lib", user_id)
    if not history_url or not favorites_url:
        return None

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Acompanhando",
                    web_app=WebAppInfo(url=history_url),
                ),
                InlineKeyboardButton(
                    "Favoritos",
                    web_app=WebAppInfo(url=favorites_url),
                ),
            ]
        ]
    )


def _caption(
    *,
    user_id: int,
    name: str,
    username: str,
    favorites_count: int,
    chapters_read_count: int,
    opened_titles_count: int,
    pages_read_count: int,
    has_keyboard: bool,
) -> str:
    user_line = f"@{html.escape(username)}" if username else f"<code>{user_id}</code>"
    hint = (
        "Use os botões abaixo para abrir suas leituras e favoritos no WebApp."
        if has_keyboard
        else "No privado com o bot eu mostro os botões do WebApp."
    )

    return (
        "👤 <b>SEU PERFIL</b>\n\n"
        f"💮 <b>ID:</b> <code>{user_id}</code>\n"
        f"🏮 <b>Nome:</b> {html.escape(name or 'Usuario')}\n\n"
        "📊 <b>Estatísticas do WebApp</b>\n"
        f"⭐ <b>Favoritos:</b> {favorites_count}\n"
        f"✅ <b>Caps lidos:</b> {chapters_read_count}\n"
        f"📚 <b>Obras abertas:</b> {opened_titles_count}\n"
        f"📄 <b>Páginas lidas:</b> {pages_read_count:n}\n\n"
        f"{hint}"
    )


async def mperfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_channel_membership(update, context):
        return

    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if not user or not message:
        return

    stats = await run_sync(get_webapp_profile_stats, user.id, 5)
    avatar_bytes = await _download_user_avatar(context, user.id)

    full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip()
    full_name = full_name or user.username or "Usuario"

    favorites_count = int(stats.get("favorites_count") or 0)
    chapters_read_count = int(stats.get("chapters_read_count") or 0)
    opened_titles_count = int(stats.get("opened_titles_count") or 0)
    pages_read_count = int(stats.get("pages_read_count") or 0)

    card = await run_sync(
        build_profile_card,
        user_id=user.id,
        name=full_name,
        username=user.username or "",
        avatar_bytes=avatar_bytes,
        brand=BOT_BRAND or "Mangas Brasil",
        following_count=opened_titles_count,
        favorites_count=favorites_count,
        chapters_read_count=chapters_read_count,
        recent_reads=stats.get("recent_reads") or [],
    )

    is_private = bool(chat and chat.type == "private")
    keyboard = _profile_keyboard(user.id) if is_private else None
    await message.reply_photo(
        photo=card,
        caption=_caption(
            user_id=user.id,
            name=full_name,
            username=user.username or "",
            favorites_count=favorites_count,
            chapters_read_count=chapters_read_count,
            opened_titles_count=opened_titles_count,
            pages_read_count=pages_read_count,
            has_keyboard=is_private and keyboard is not None,
        ),
        parse_mode="HTML",
        reply_markup=keyboard,
    )
