from __future__ import annotations

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from config import ADMIN_IDS
from services.control_blocklist import is_blocked

BLOCKED_TEXT = (
    "?? <b>Acesso bloqueado</b>\n\n"
    "Voc? foi bloqueado de usar este bot.\n"
    "Se acredita que isso foi um erro, entre em contato com o suporte."
)


def _is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


async def control_block_message_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or _is_admin(user.id) or not is_blocked(user.id):
        return
    if message:
        await message.reply_text(BLOCKED_TEXT, parse_mode="HTML")
    raise ApplicationHandlerStop


async def control_block_callback_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or _is_admin(user.id) or not is_blocked(user.id):
        return
    await query.answer("Voc? foi bloqueado de usar este bot.", show_alert=True)
    raise ApplicationHandlerStop
