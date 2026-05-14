import html
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import REQUIRED_CHANNELS, REQUIRED_CHANNEL_URL

_MEMBERSHIP_CACHE: dict[int, tuple[bool, float]] = {}
_MEMBER_TTL = 300
_NON_MEMBER_TTL = 60


def _cache_get(user_id: int) -> bool | None:
    item = _MEMBERSHIP_CACHE.get(user_id)
    if not item:
        return None
    allowed, expires_at = item
    if time.time() >= expires_at:
        _MEMBERSHIP_CACHE.pop(user_id, None)
        return None
    return allowed


def _cache_set(user_id: int, allowed: bool) -> None:
    ttl = _MEMBER_TTL if allowed else _NON_MEMBER_TTL
    _MEMBERSHIP_CACHE[user_id] = (allowed, time.time() + ttl)


def _is_member_allowed(member) -> bool:
    status = str(getattr(member, "status", "") or "").strip().lower()
    if status in {"member", "administrator", "creator"}:
        return True
    return status == "restricted" and bool(getattr(member, "is_member", False))


async def _is_user_in_all_required_channels(bot, user_id: int) -> bool:
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(channel, user_id)
        except Exception:
            return False
        if not _is_member_allowed(member):
            return False
    return True


def _gate_text(first_name: str | None) -> str:
    name = html.escape(first_name or "amigo")
    return (
        f"🛑 <b>Calma aí, {name}</b>\n\n"
        "Para usar este comando, você precisa entrar nos meus canais primeiro.\n\n"
        "Assim você fica por dentro das novidades, avisos e atualizações.\n\n"
        "Clique abaixo, entre nos canais da pasta e volte para tentar novamente."
    )


async def ensure_channel_membership(update, context: ContextTypes.DEFAULT_TYPE):
    if not REQUIRED_CHANNELS:
        return True

    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False

    cached = _cache_get(user.id)
    if cached is True:
        return True

    allowed = await _is_user_in_all_required_channels(context.bot, user.id)
    _cache_set(user.id, allowed)
    if allowed:
        return True

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📢 Entrar nos canais", url=REQUIRED_CHANNEL_URL)]]
    )

    await message.reply_text(
        _gate_text(user.first_name),
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return False
