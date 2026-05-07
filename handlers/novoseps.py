import html
import json
import os
import re
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes

from config import ADMIN_IDS, AUTO_POST_LIMIT, BOT_USERNAME, CANAL_POSTAGEM_CAPITULOS, DATA_DIR
from core.channel_target import ensure_channel_target
from handlers.callbacks import _ALLOWED_TAG_TRANSLATIONS, _norm_tag_label
from services.catalog_client import get_recent_chapters

FORCED_CHANNEL_TARGET = CANAL_POSTAGEM_CAPITULOS or "@AtualizacoesOn"
POSTED_JSON_PATH = Path(DATA_DIR) / "capitulos_postados.json"
POSTED_KEEP_LIMIT = 1000
AUTO_POST_PERMISSION_COOLDOWN = 30 * 60
_AUTO_POST_DISABLED_UNTIL = 0.0


def _is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def _load_posted() -> list[str]:
    if not POSTED_JSON_PATH.exists():
        return []
    try:
        data = json.loads(POSTED_JSON_PATH.read_text(encoding="utf-8"))
        return [str(item).strip() for item in data if str(item).strip()]
    except Exception:
        return []


def _save_posted(items: list[str]) -> None:
    deduped = list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))
    POSTED_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = POSTED_JSON_PATH.with_suffix(POSTED_JSON_PATH.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(deduped[-POSTED_KEEP_LIMIT:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, POSTED_JSON_PATH)


def _deep_link(chapter_id: str, title_id: str = "") -> str:
    resolved_chapter_id = str(chapter_id).strip()
    return f"https://t.me/{BOT_USERNAME}?start=ch_{resolved_chapter_id}"


def _title_link(title_id: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=title_{title_id}"


def _post_key(item: dict) -> str:
    chapter_id = str(item.get("chapter_id") or "").strip()
    title_id = str(item.get("title_id") or "").strip()
    if title_id and chapter_id:
        return f"{title_id}:{chapter_id}"
    return chapter_id


def _display_title(item: dict) -> str:
    return item.get("display_title") or item.get("title") or "Mangá"


def _genre_hashtags(item: dict, limit: int = 6) -> str:
    raw = item.get("genres") or item.get("anilist_genres") or []
    if isinstance(raw, str):
        values = [part.strip() for part in re.split(r"[,|/•]+|\s+(?=#)", raw)]
    else:
        values = list(raw)

    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            value = value.get("name") or value.get("label") or value.get("title") or ""
        norm = _norm_tag_label(str(value).strip().lstrip("#"))
        translated = _ALLOWED_TAG_TRANSLATIONS.get(norm)
        if not translated or norm in seen:
            continue
        seen.add(norm)
        tag = "#" + re.sub(r"[^\w]+", "", translated[0].replace(" ", "_"), flags=re.UNICODE)
        if tag != "#":
            output.append(tag)
        if len(output) >= limit:
            break

    return ", ".join(output) if output else "Não informado"


def _caption(item: dict) -> str:
    title = html.escape(_display_title(item))
    chapter_number = html.escape(str(item.get("chapter_number") or "?"))
    updated_at = html.escape(item.get("updated_at") or "agora há pouco")
    genres = html.escape(_genre_hashtags(item))
    channel = html.escape(FORCED_CHANNEL_TARGET)
    return "\n".join(
        [
            f"📚 <b>{title}</b>",
            "",
            f"<blockquote><b>Capítulo:</b> <i>{chapter_number}</i>",
            f"<b>Atualizado:</b> <i>{updated_at}</i>",
            f"<b>Gêneros:</b> <i>{genres}</i></blockquote>",
            "",
            f"» <b>{channel}</b>",
        ]
    )


def _keyboard(item: dict) -> InlineKeyboardMarkup | None:
    chapter_id = str(item.get("chapter_id") or "").strip()
    title_id = str(item.get("title_id") or "").strip()
    if chapter_id:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("📖 Ler capítulo", url=_deep_link(chapter_id, title_id))]]
        )
    if title_id:
        return InlineKeyboardMarkup([[InlineKeyboardButton("📚 Abrir obra", url=_title_link(title_id))]])
    return None


def _is_permission_error(error: Exception) -> bool:
    text = str(error).lower()
    return isinstance(error, (BadRequest, Forbidden)) and (
        "administrator rights" in text
        or "not enough rights" in text
        or "chat not found" in text
        or "forbidden" in text
    )


async def _send_recent_chapter(bot, chat_id, item: dict) -> None:
    cover = item.get("cover_url") or item.get("background_url") or None
    caption = _caption(item)
    keyboard = _keyboard(item)

    if cover:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=cover,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        except Exception as error:
            if _is_permission_error(error):
                raise
            print("ERRO POST NOVO CAP FOTO:", repr(error), item.get("chapter_id"), item.get("title"))

    await bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def _post_recent_items(bot, destination, items: list[dict], posted: list[str]) -> tuple[int, int, list[str]]:
    posted_set = set(posted)
    sent = 0
    failed = 0

    for item in reversed(list(items)):
        key = _post_key(item)
        if not key or key in posted_set:
            continue
        try:
            await _send_recent_chapter(bot, destination, item)
        except Exception as error:
            if _is_permission_error(error):
                raise
            failed += 1
            print("ERRO POST NOVO CAP:", repr(error), item.get("chapter_id"), item.get("title"))
            continue

        posted.append(key)
        posted_set.add(key)
        sent += 1

    return sent, failed, posted


async def postnovoseps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not _is_admin(user.id):
        if message:
            await message.reply_text("❌ <b>Você não tem permissão para usar esse comando.</b>", parse_mode="HTML")
        return

    status_message = await message.reply_text(
        f"📤 <b>Buscando capítulos novos para enviar em {html.escape(FORCED_CHANNEL_TARGET)}...</b>",
        parse_mode="HTML",
    )

    try:
        items = await get_recent_chapters(limit=AUTO_POST_LIMIT)
        if not items:
            await status_message.edit_text(
                "⚠️ <b>Não encontrei capítulos recentes para postar.</b>",
                parse_mode="HTML",
            )
            return

        destination = await ensure_channel_target(context.bot, FORCED_CHANNEL_TARGET)
        posted = _load_posted()
        sent, failed, posted = await _post_recent_items(context.bot, destination, items, posted)
        _save_posted(posted)

        await status_message.edit_text(
            "✅ <b>Postagem concluída.</b>\n\n"
            f"<b>Canal:</b> <code>{html.escape(FORCED_CHANNEL_TARGET)}</code>\n"
            f"<b>Novos capítulos enviados:</b> <code>{sent}</code>\n"
            f"<b>Falhas:</b> <code>{failed}</code>",
            parse_mode="HTML",
        )
    except Exception as error:
        if _is_permission_error(error):
            await status_message.edit_text(
                "⚠️ <b>Não consegui postar no canal.</b>\n\n"
                f"O bot precisa ser administrador em <code>{html.escape(FORCED_CHANNEL_TARGET)}</code> "
                "com permissão para enviar mensagens e fotos.",
                parse_mode="HTML",
            )
            return
        print("ERRO POSTNOVOSEPS:", repr(error))
        await status_message.edit_text(
            "⚠️ <b>Não consegui concluir as atualizações agora.</b>\n\n"
            f"<code>{html.escape(str(error) or 'Tente novamente em instantes.')}</code>",
            parse_mode="HTML",
        )


async def auto_post_new_eps_job(context: ContextTypes.DEFAULT_TYPE):
    global _AUTO_POST_DISABLED_UNTIL
    if time.time() < _AUTO_POST_DISABLED_UNTIL:
        return

    try:
        destination = await ensure_channel_target(context.bot, FORCED_CHANNEL_TARGET)
        items = await get_recent_chapters(limit=AUTO_POST_LIMIT)
        if not items:
            return

        posted = _load_posted()
        sent, failed, posted = await _post_recent_items(context.bot, destination, items, posted)
        if sent or failed:
            _save_posted(posted)
    except Exception as error:
        if _is_permission_error(error):
            _AUTO_POST_DISABLED_UNTIL = time.time() + AUTO_POST_PERMISSION_COOLDOWN
            print(
                "ERRO AUTO POST PERMISSAO:",
                f"bot sem permissao em {FORCED_CHANNEL_TARGET}; pausando por {AUTO_POST_PERMISSION_COOLDOWN}s",
            )
            return
        print("ERRO AUTO POST NOVO CAP:", repr(error))
