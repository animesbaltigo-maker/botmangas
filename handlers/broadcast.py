from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from config import ADMIN_IDS
from services.user_registry import get_all_users, get_total_users, remove_user


LOGGER = logging.getLogger(__name__)

BROADCAST_STATE_KEY = "broadcast_state"
BROADCAST_DATA_KEY = "broadcast_data"
BROADCAST_LOCK_KEY = "broadcast_lock"
BROADCAST_LAST_KEY = "broadcast_last_action"
BROADCAST_PANEL_KEY = "broadcast_panel"

BROADCAST_COOLDOWN = 1.0

SEND_WORKERS = 4
PER_MESSAGE_DELAY = 0.05
STATUS_EVERY = 50
STATUS_MIN_INTERVAL = 2.0
PIN_WARN_USERS = 20
PIN_MAX_USERS = 100
TEMPLATE_LIMIT = 12

GLOBAL_BROADCAST_RUNNING_KEY = "broadcast_running"
GLOBAL_BROADCAST_TASK_KEY = "broadcast_task"
GLOBAL_BROADCAST_CONTROL_KEY = "broadcast_control"
GLOBAL_BROADCAST_PUBLIC_ALERTS_KEY = "broadcast_public_alerts"

TEMPLATES_PATH = Path("data") / "broadcast_templates.json"


def _is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def _escape(value: object) -> str:
    return html.escape(str(value or ""))


def _yes_no(value: bool) -> str:
    return "Sim" if value else "NÃ£o"


def _blank_data() -> dict[str, object]:
    return {
        "mode": None,
        "target_user_id": None,
        "text": "",
        "source_chat_id": None,
        "source_message_id": None,
        "has_media": False,
        "media_type": None,
        "media_file_id": None,
        "button_rows": [],
        "draft_button_text": "",
        "pin": False,
        "schedule_at": None,
        "confirm_pin": False,
    }


def _panel_data(context: ContextTypes.DEFAULT_TYPE) -> dict[str, object]:
    data = context.user_data.get(BROADCAST_DATA_KEY)
    if not isinstance(data, dict):
        data = _blank_data()
        context.user_data[BROADCAST_DATA_KEY] = data
    return data


def _set_state(context: ContextTypes.DEFAULT_TYPE, state: str) -> None:
    context.user_data[BROADCAST_STATE_KEY] = state


def _get_state(context: ContextTypes.DEFAULT_TYPE) -> str:
    return str(context.user_data.get(BROADCAST_STATE_KEY, "") or "")


def _clear_transient_fields(data: dict[str, object]) -> None:
    data["draft_button_text"] = ""


def _reset_broadcast(context: ContextTypes.DEFAULT_TYPE, *, keep_panel: bool = False) -> None:
    context.user_data.pop(BROADCAST_STATE_KEY, None)
    context.user_data.pop(BROADCAST_DATA_KEY, None)
    context.user_data.pop(BROADCAST_LOCK_KEY, None)
    context.user_data.pop(BROADCAST_LAST_KEY, None)
    if not keep_panel:
        context.user_data.pop(BROADCAST_PANEL_KEY, None)


def _remember_panel_message(context: ContextTypes.DEFAULT_TYPE, message: Message, *, kind: str) -> None:
    context.user_data[BROADCAST_PANEL_KEY] = {
        "chat_id": int(message.chat_id),
        "message_id": int(message.message_id),
        "kind": kind,
    }


def _panel_ref(context: ContextTypes.DEFAULT_TYPE) -> dict[str, object] | None:
    ref = context.user_data.get(BROADCAST_PANEL_KEY)
    return ref if isinstance(ref, dict) else None


async def _delete_message_safely(message: Message | None) -> None:
    if not message:
        return
    try:
        await message.delete()
    except Exception:
        LOGGER.debug("NÃ£o foi possÃ­vel apagar mensagem do broadcast", exc_info=True)


async def _guard_action(context: ContextTypes.DEFAULT_TYPE) -> str:
    now = time.monotonic()
    if context.user_data.get(BROADCAST_LOCK_KEY):
        return "locked"

    last = float(context.user_data.get(BROADCAST_LAST_KEY, 0.0) or 0.0)
    if now - last < BROADCAST_COOLDOWN:
        return "cooldown"

    context.user_data[BROADCAST_LOCK_KEY] = True
    context.user_data[BROADCAST_LAST_KEY] = now
    return "ok"


def _release_guard(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(BROADCAST_LOCK_KEY, None)


def _broadcast_is_running(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.application.bot_data.get(GLOBAL_BROADCAST_RUNNING_KEY, False))


def _set_broadcast_running(context: ContextTypes.DEFAULT_TYPE, value: bool) -> None:
    context.application.bot_data[GLOBAL_BROADCAST_RUNNING_KEY] = value


def _set_broadcast_task(context: ContextTypes.DEFAULT_TYPE, task: asyncio.Task[object] | None) -> None:
    if task is None:
        context.application.bot_data.pop(GLOBAL_BROADCAST_TASK_KEY, None)
        return
    context.application.bot_data[GLOBAL_BROADCAST_TASK_KEY] = task


def _broadcast_control(context: ContextTypes.DEFAULT_TYPE) -> dict[str, object]:
    control = context.application.bot_data.get(GLOBAL_BROADCAST_CONTROL_KEY)
    if not isinstance(control, dict):
        control = {"paused": False, "cancelled": False}
        context.application.bot_data[GLOBAL_BROADCAST_CONTROL_KEY] = control
    return control


def _mode_label(mode: str | None) -> str:
    if mode == "all":
        return "Todos os usuÃ¡rios"
    if mode == "single":
        return "UsuÃ¡rio especÃ­fico"
    return "NÃ£o definido"


def _format_line(label: str, value: str) -> str:
    return f"<b>{_escape(label)}:</b> {value}"


def _content_ready(data: dict[str, object]) -> bool:
    return bool(str(data.get("text") or "").strip() or data.get("has_media"))


def _button_rows_data(data: dict[str, object]) -> list[list[dict[str, str]]]:
    rows = data.get("button_rows")
    if isinstance(rows, list):
        return rows  # type: ignore[return-value]
    return []


def _button_count(data: dict[str, object]) -> int:
    return sum(len(row) for row in _button_rows_data(data))


def _schedule_label(data: dict[str, object]) -> str:
    value = data.get("schedule_at")
    if not value:
        return "NÃ£o"
    try:
        dt = datetime.fromtimestamp(float(value))
    except Exception:
        return "Sim"
    return dt.strftime("%d/%m/%Y %H:%M")


def _main_menu_text(data: dict[str, object], *, running: bool, note: str | None = None) -> str:
    mode = str(data.get("mode") or "")
    target_user_id = data.get("target_user_id")
    has_media = bool(data.get("has_media"))
    text = str(data.get("text") or "")
    pin = bool(data.get("pin"))

    lines = [
        "ðŸ“¬ <b>Painel de transmissÃ£o</b>",
        "",
        "ðŸŸ¢ <b>Status:</b> <code>Broadcast em andamento</code>" if running else "âšªï¸ <b>Status:</b> <code>Parado</code>",
        "<i>Monte, teste, agende e acompanhe seus envios com seguranÃ§a.</i>",
        "",
    ]
    if note:
        lines.extend([f"<blockquote>{note}</blockquote>", ""])

    details = [
        _format_line("Destino", f"<code>{_escape(_mode_label(mode))}</code>"),
    ]
    if mode == "single" and target_user_id:
        details.append(_format_line("ID alvo", f"<code>{_escape(target_user_id)}</code>"))
    details.extend(
        [
            _format_line("MÃ­dia", f"<code>{_yes_no(has_media)}</code>"),
            _format_line("Texto", f"<code>{_yes_no(bool(text.strip()))}</code>"),
            _format_line("BotÃµes", f"<code>{_button_count(data)}</code>"),
            _format_line("Fixar", f"<code>{_yes_no(pin)}</code>"),
            _format_line("Agendado", f"<code>{_escape(_schedule_label(data))}</code>"),
            _format_line("UsuÃ¡rios salvos", f"<code>{get_total_users()}</code>"),
        ]
    )
    lines.append("<blockquote>" + "\n".join(details) + "</blockquote>")
    lines.extend(["", "Escolha uma opÃ§Ã£o abaixo."])
    return "\n".join(lines)


def _main_menu_keyboard(data: dict[str, object], *, running: bool) -> InlineKeyboardMarkup:
    mode = str(data.get("mode") or "")
    if mode == "all":
        mode_label = "ðŸŒ Todos"
    elif mode == "single":
        mode_label = "ðŸ‘¤ UsuÃ¡rio"
    else:
        mode_label = "ðŸŽ¯ Destino"

    send_label = "â³ Rodando" if running else "ðŸš€ Enviar"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(mode_label, callback_data="bc|set_mode"),
                InlineKeyboardButton("ðŸ–¼ MÃ­dia", callback_data="bc|set_media"),
            ],
            [
                InlineKeyboardButton("ðŸ“ Mensagem", callback_data="bc|set_text"),
                InlineKeyboardButton(f"ðŸ”˜ BotÃµes ({_button_count(data)})", callback_data="bc|set_buttons"),
            ],
            [
                InlineKeyboardButton("ðŸ§ª Enviar teste", callback_data="bc|test_send"),
                InlineKeyboardButton("ðŸ—“ Agendar", callback_data="bc|schedule"),
            ],
            [
                InlineKeyboardButton("ðŸ’¾ Salvar modelo", callback_data="bc|save_template"),
                InlineKeyboardButton("ðŸ“š Usar modelo", callback_data="bc|use_template"),
            ],
            [
                InlineKeyboardButton(f"ðŸ“Œ {_yes_no(bool(data.get('pin')))}", callback_data="bc|toggle_pin"),
                InlineKeyboardButton("ðŸ‘€ PrÃ©via", callback_data="bc|preview"),
            ],
            [
                InlineKeyboardButton(send_label, callback_data="bc|send"),
                InlineKeyboardButton("ðŸ—‘ Limpar", callback_data="bc|reset"),
            ],
            [InlineKeyboardButton("âŒ Fechar", callback_data="bc|close")],
        ]
    )


def _running_keyboard(control: dict[str, object]) -> InlineKeyboardMarkup:
    paused = bool(control.get("paused"))
    first = InlineKeyboardButton("â–¶ï¸ Continuar", callback_data="bc|resume") if paused else InlineKeyboardButton("â¸ Pausar", callback_data="bc|pause")
    return InlineKeyboardMarkup([[first, InlineKeyboardButton("ðŸ›‘ Cancelar", callback_data="bc|cancel_running")]])


def _mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("ðŸŒ Todos", callback_data="bc|mode_all")],
            [InlineKeyboardButton("ðŸ‘¤ UsuÃ¡rio especÃ­fico", callback_data="bc|mode_single")],
            [InlineKeyboardButton("ðŸ”™ Voltar", callback_data="bc|menu")],
        ]
    )


def _prompt_keyboard(remove_callback: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if remove_callback:
        labels = {
            "bc|remove_media": "ðŸ—‘ Remover mÃ­dia",
            "bc|remove_text": "ðŸ—‘ Remover texto",
            "bc|remove_buttons": "ðŸ—‘ Remover botÃµes",
            "bc|remove_schedule": "ðŸ—‘ Remover agendamento",
        }
        rows.append([InlineKeyboardButton(labels.get(remove_callback, "ðŸ—‘ Remover"), callback_data=remove_callback)])
    rows.append([InlineKeyboardButton("ðŸ”™ Voltar", callback_data="bc|menu")])
    return InlineKeyboardMarkup(rows)


def _templates_keyboard(templates: list[dict[str, object]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for idx, template in enumerate(templates[:TEMPLATE_LIMIT]):
        name = str(template.get("name") or f"Modelo {idx + 1}")[:32]
        rows.append([InlineKeyboardButton(f"ðŸ“Œ {name}", callback_data=f"bc|load_template|{idx}")])
    rows.append([InlineKeyboardButton("ðŸ”™ Voltar", callback_data="bc|menu")])
    return InlineKeyboardMarkup(rows)


def _message_text_from(message: Message) -> str:
    value = getattr(message, "text_html", None) or getattr(message, "caption_html", None)
    if value:
        return str(value).strip()
    return _escape(message.text or message.caption or "").strip()


def _load_templates() -> list[dict[str, object]]:
    try:
        raw = json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return raw if isinstance(raw, list) else []


def _save_templates(templates: list[dict[str, object]]) -> None:
    TEMPLATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    TEMPLATES_PATH.write_text(json.dumps(templates[:TEMPLATE_LIMIT], ensure_ascii=False, indent=2), encoding="utf-8")


def _template_payload(data: dict[str, object], name: str) -> dict[str, object]:
    return {
        "name": name,
        "text": str(data.get("text") or ""),
        "source_chat_id": data.get("source_chat_id"),
        "source_message_id": data.get("source_message_id"),
        "has_media": bool(data.get("has_media")),
        "media_type": data.get("media_type"),
        "media_file_id": data.get("media_file_id"),
        "button_rows": _button_rows_data(data),
        "pin": bool(data.get("pin")),
    }


def _apply_template(data: dict[str, object], template: dict[str, object]) -> None:
    for key in ("text", "source_chat_id", "source_message_id", "has_media", "media_type", "media_file_id", "button_rows", "pin"):
        data[key] = template.get(key, _blank_data().get(key))


def _parse_when(raw: str) -> float | None:
    text = raw.strip().lower()
    now = datetime.now()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return None
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        return dt.timestamp()

    match = re.fullmatch(r"(hoje|amanh[Ã£a])\s+(\d{1,2}):(\d{2})", text)
    if match:
        hour, minute = int(match.group(2)), int(match.group(3))
        if hour > 23 or minute > 59:
            return None
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if match.group(1).startswith("amanh"):
            dt += timedelta(days=1)
        if dt <= now:
            return None
        return dt.timestamp()

    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if dt > now:
            return dt.timestamp()
    return None


def _parse_buttons(raw: str) -> tuple[list[list[dict[str, str]]], str | None]:
    rows: list[list[dict[str, str]]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        row: list[dict[str, str]] = []
        for part in re.split(r"\s+&&\s+", line):
            match = re.match(r"^(.+?)\s+-\s+(.+)$", part.strip())
            if not match:
                return [], "Use o formato: Texto do botÃ£o - link"
            label = match.group(1).strip()
            value = match.group(2).strip()
            if not label or len(label) > 64:
                return [], "O texto de cada botÃ£o precisa ter atÃ© 64 caracteres."
            lowered = value.lower()
            if lowered.startswith(("popup:", "alert:")):
                payload = value.split(":", 1)[1].strip()
                if not payload:
                    return [], "O popup precisa ter um texto."
                row.append({"type": "alert", "text": label, "value": payload[:180]})
            elif lowered.startswith("share:"):
                payload = value.split(":", 1)[1].strip()
                if not payload:
                    return [], "O botÃ£o de compartilhamento precisa ter um texto ou link."
                row.append({"type": "url", "text": label, "value": "https://t.me/share/url?url=" + quote_plus(payload)})
            else:
                if value.startswith("t.me/"):
                    value = "https://" + value
                if not value.startswith(("http://", "https://", "tg://")):
                    return [], "Links precisam comeÃ§ar com http://, https://, tg:// ou t.me/"
                row.append({"type": "url", "text": label, "value": value})
        rows.append(row)
    if not rows:
        return [], "Envie pelo menos um botÃ£o."
    if sum(len(row) for row in rows) > 16:
        return [], "Use no mÃ¡ximo 16 botÃµes por transmissÃ£o."
    return rows, None


def _public_alerts(context: ContextTypes.DEFAULT_TYPE) -> dict[str, str]:
    alerts = context.application.bot_data.get(GLOBAL_BROADCAST_PUBLIC_ALERTS_KEY)
    if not isinstance(alerts, dict):
        alerts = {}
        context.application.bot_data[GLOBAL_BROADCAST_PUBLIC_ALERTS_KEY] = alerts
    return alerts


def _message_keyboard(context: ContextTypes.DEFAULT_TYPE, data: dict[str, object]) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    alerts = _public_alerts(context)
    for row in _button_rows_data(data):
        built_row: list[InlineKeyboardButton] = []
        for button in row:
            label = str(button.get("text") or "")[:64]
            value = str(button.get("value") or "")
            if button.get("type") == "alert":
                token = uuid.uuid5(uuid.NAMESPACE_URL, value).hex[:12]
                alerts[token] = value[:180]
                built_row.append(InlineKeyboardButton(label, callback_data=f"bc_public|alert|{token}"))
            else:
                built_row.append(InlineKeyboardButton(label, url=value))
        if built_row:
            rows.append(built_row)
    return InlineKeyboardMarkup(rows) if rows else None


def _preview_text(data: dict[str, object]) -> str:
    text = str(data.get("text") or "").strip()
    lines = [
        "ðŸ‘€ <b>PrÃ©via da transmissÃ£o</b>",
        "",
        _format_line("Destino", f"<code>{_escape(_mode_label(str(data.get('mode') or '')))}</code>"),
    ]
    if data.get("mode") == "single" and data.get("target_user_id"):
        lines.append(_format_line("ID alvo", f"<code>{_escape(data.get('target_user_id'))}</code>"))
    lines.extend(
        [
            _format_line("MÃ­dia", f"<code>{_yes_no(bool(data.get('has_media')))}</code>"),
            _format_line("BotÃµes", f"<code>{_button_count(data)}</code>"),
            _format_line("Fixar", f"<code>{_yes_no(bool(data.get('pin')))}</code>"),
            _format_line("Agendado", f"<code>{_escape(_schedule_label(data))}</code>"),
        ]
    )
    return "\n".join(lines) + "\n\n" + (text or "<i>Sem texto.</i>")


def _preview_keyboard(data: dict[str, object]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("ðŸ§ª Enviar teste", callback_data="bc|test_send")],
            [InlineKeyboardButton("ðŸš€ Confirmar envio", callback_data="bc|send")],
            [InlineKeyboardButton("ðŸ”™ Voltar", callback_data="bc|menu")],
        ]
    )


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("âŒ NÃƒO", callback_data="bc|menu"),
                InlineKeyboardButton("âœ… SIM", callback_data="bc|confirm_send"),
            ]
        ]
    )


async def _send_panel_text(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    reply_to_message_id: int | None,
    text: str,
    reply_markup,
) -> Message:
    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
        reply_to_message_id=reply_to_message_id,
    )
    _remember_panel_message(context, sent, kind="text")
    return sent


async def _render_panel_text(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup,
    *,
    query_message: Message | None = None,
    source_message: Message | None = None,
) -> Message | None:
    if query_message:
        if query_message.photo or query_message.video or query_message.document or query_message.animation:
            sent = await _send_panel_text(
                context,
                chat_id=int(query_message.chat_id),
                reply_to_message_id=None,
                text=text,
                reply_markup=reply_markup,
            )
            await _delete_message_safely(query_message)
            return sent
        try:
            await query_message.edit_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            _remember_panel_message(context, query_message, kind="text")
            return query_message
        except Exception:
            LOGGER.debug("Falha ao editar painel via callback", exc_info=True)

    panel = _panel_ref(context)
    if panel:
        try:
            if str(panel.get("kind") or "text") == "media":
                sent = await _send_panel_text(
                    context,
                    chat_id=int(panel["chat_id"]),
                    reply_to_message_id=None,
                    text=text,
                    reply_markup=reply_markup,
                )
                try:
                    await context.bot.delete_message(chat_id=int(panel["chat_id"]), message_id=int(panel["message_id"]))
                except Exception:
                    LOGGER.debug("Falha ao apagar painel antigo com mÃ­dia", exc_info=True)
                return sent

            await context.bot.edit_message_text(
                chat_id=int(panel["chat_id"]),
                message_id=int(panel["message_id"]),
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return None
        except Exception:
            LOGGER.debug("Falha ao editar painel salvo", exc_info=True)

    if source_message:
        return await _send_panel_text(
            context,
            chat_id=int(source_message.chat_id),
            reply_to_message_id=None,
            text=text,
            reply_markup=reply_markup,
        )
    return None


def _with_note(base_text: str, note: str | None = None) -> str:
    if not note:
        return base_text
    return f"{base_text}\n\n<blockquote>{note}</blockquote>"


async def _show_main_menu(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    query_message: Message | None = None,
    source_message: Message | None = None,
    note: str | None = None,
) -> Message | None:
    data = _panel_data(context)
    _set_state(context, "")
    _clear_transient_fields(data)
    return await _render_panel_text(
        context,
        _main_menu_text(data, running=_broadcast_is_running(context), note=note),
        _main_menu_keyboard(data, running=_broadcast_is_running(context)),
        query_message=query_message,
        source_message=source_message,
    )


async def _show_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    prompt_text: str,
    *,
    remove_callback: str | None = None,
    query_message: Message | None = None,
    source_message: Message | None = None,
    note: str | None = None,
) -> Message | None:
    return await _render_panel_text(
        context,
        _with_note(prompt_text, note),
        _prompt_keyboard(remove_callback),
        query_message=query_message,
        source_message=source_message,
    )


async def _show_preview(context: ContextTypes.DEFAULT_TYPE, *, query_message: Message) -> None:
    await _render_panel_text(context, _preview_text(_panel_data(context)), _preview_keyboard(_panel_data(context)), query_message=query_message)


async def _send_broadcast_message(bot, context: ContextTypes.DEFAULT_TYPE, chat_id: int, data: dict[str, object]) -> Message:
    text = str(data.get("text") or "").strip()
    reply_markup = _message_keyboard(context, data)
    media_type = str(data.get("media_type") or "")
    media_file_id = data.get("media_file_id")
    source_chat_id = data.get("source_chat_id")
    source_message_id = data.get("source_message_id")

    if media_file_id:
        kwargs = {
            "chat_id": chat_id,
            media_type: str(media_file_id),
            "reply_markup": reply_markup,
        }
        if media_type not in {"sticker", "video_note"}:
            kwargs["caption"] = text or None
            kwargs["parse_mode"] = ParseMode.HTML if text else None
        method = {
            "photo": bot.send_photo,
            "video": bot.send_video,
            "document": bot.send_document,
            "animation": bot.send_animation,
            "audio": bot.send_audio,
            "voice": bot.send_voice,
            "sticker": bot.send_sticker,
            "video_note": bot.send_video_note,
        }.get(media_type)
        if method:
            sent = await method(**kwargs)
            if media_type in {"sticker", "video_note"} and (text or reply_markup):
                return await bot.send_message(
                    chat_id=chat_id,
                    text=text or "ðŸ“¢",
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            return sent

    if source_chat_id and source_message_id:
        return await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=int(source_chat_id),
            message_id=int(source_message_id),
            caption=text or None,
            parse_mode=ParseMode.HTML if text else None,
            reply_markup=reply_markup,
        )

    return await bot.send_message(
        chat_id=chat_id,
        text=text or "ðŸ“¢",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


async def _maybe_pin_message(bot, chat_id: int, message: Message | None, should_pin: bool) -> None:
    if not should_pin or not message:
        return
    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=message.message_id, disable_notification=True)
    except Exception:
        LOGGER.debug("Falha ao fixar mensagem do broadcast", exc_info=True)


def _should_remove_user_on_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "blocked" in text
        or "chat not found" in text
        or "user is deactivated" in text
        or "bot was blocked by the user" in text
        or "forbidden" in text
    )


async def _safe_send_one(bot, context: ContextTypes.DEFAULT_TYPE, user_id: int, data: dict[str, object], should_pin: bool) -> tuple[bool, bool]:
    try:
        message = await _send_broadcast_message(bot, context, user_id, data)
        await _maybe_pin_message(bot, user_id, message, should_pin)
        return True, False
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 1.0)
        try:
            message = await _send_broadcast_message(bot, context, user_id, data)
            await _maybe_pin_message(bot, user_id, message, should_pin)
            return True, False
        except Exception as retry_exc:
            return False, _should_remove_user_on_error(retry_exc)
    except (Forbidden, BadRequest) as exc:
        return False, _should_remove_user_on_error(exc)
    except (TimedOut, NetworkError):
        return False, False
    except Exception as exc:
        return False, _should_remove_user_on_error(exc)


async def _send_test(context: ContextTypes.DEFAULT_TYPE, admin_id: int, data: dict[str, object]) -> tuple[bool, str]:
    if not _content_ready(data):
        return False, "Defina uma mensagem ou mÃ­dia antes do teste."
    ok, _ = await _safe_send_one(context.bot, context, admin_id, data, False)
    return ok, "Teste enviado para vocÃª." if ok else "NÃ£o consegui enviar o teste."


def _progress_text(counters: dict[str, Any], total: int, *, paused: bool = False, cancelled: bool = False) -> str:
    remaining = max(0, total - int(counters["processed"]))
    title = "â¸ <b>TransmissÃ£o pausada</b>" if paused else "ðŸ›‘ <b>Cancelando transmissÃ£o...</b>" if cancelled else "ðŸš€ <b>TransmissÃ£o em andamento...</b>"
    return (
        f"{title}\n\n"
        f"âœ… <b>Enviadas:</b> <code>{int(counters['sent'])}</code>\n"
        f"âŒ <b>Falhas:</b> <code>{int(counters['failed'])}</code>\n"
        f"ðŸ“¦ <b>Processadas:</b> <code>{int(counters['processed'])}/{total}</code>\n"
        f"â³ <b>Restantes:</b> <code>{remaining}</code>"
    )


async def _update_status_message(context: ContextTypes.DEFAULT_TYPE, status_msg: Message, counters: dict[str, Any], total: int) -> None:
    control = _broadcast_control(context)
    try:
        await status_msg.edit_text(
            _progress_text(counters, total, paused=bool(control.get("paused")), cancelled=bool(control.get("cancelled"))),
            parse_mode=ParseMode.HTML,
            reply_markup=_running_keyboard(control),
        )
    except Exception:
        LOGGER.debug("Falha ao atualizar status do broadcast", exc_info=True)


async def _broadcast_worker(
    queue: asyncio.Queue[int | None],
    *,
    context: ContextTypes.DEFAULT_TYPE,
    bot,
    data: dict[str, object],
    should_pin: bool,
    counters: dict[str, Any],
    status_msg: Message,
    total: int,
) -> None:
    control = _broadcast_control(context)
    while True:
        user_id = await queue.get()
        try:
            if user_id is None:
                return
            while bool(control.get("paused")) and not bool(control.get("cancelled")):
                await asyncio.sleep(1.0)
            if bool(control.get("cancelled")):
                return

            ok, should_remove = await _safe_send_one(bot, context, int(user_id), data, should_pin)
            if ok:
                counters["sent"] += 1
            else:
                counters["failed"] += 1
                if should_remove:
                    counters["remove_ids"].add(int(user_id))

            counters["processed"] += 1
            now = time.monotonic()
            should_update = (
                counters["processed"] % STATUS_EVERY == 0
                or now - float(counters["last_status_at"]) >= STATUS_MIN_INTERVAL
                or bool(control.get("paused"))
                or bool(control.get("cancelled"))
            )
            if should_update:
                async with counters["status_lock"]:
                    counters["last_status_at"] = time.monotonic()
                    await _update_status_message(context, status_msg, counters, total)

            await asyncio.sleep(PER_MESSAGE_DELAY)
        finally:
            queue.task_done()


async def _execute_broadcast_background(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    application,
    bot,
    admin_chat_id: int,
    reply_to_message_id: int | None,
    data: dict[str, object],
) -> None:
    try:
        mode = str(data.get("mode") or "")
        text = str(data.get("text") or "").strip()
        has_media = bool(data.get("has_media"))
        target_user_id = data.get("target_user_id")
        requested_pin = bool(data.get("pin"))

        if mode not in {"all", "single"}:
            await bot.send_message(chat_id=admin_chat_id, text="Defina o destino primeiro.", reply_to_message_id=reply_to_message_id)
            return

        if not text and not has_media:
            await bot.send_message(chat_id=admin_chat_id, text="Defina pelo menos uma mensagem ou uma mÃ­dia.", reply_to_message_id=reply_to_message_id)
            return

        if mode == "single":
            if not isinstance(target_user_id, int):
                await bot.send_message(chat_id=admin_chat_id, text="Envie um ID numÃ©rico vÃ¡lido.", reply_to_message_id=reply_to_message_id)
                return

            ok, should_remove = await _safe_send_one(bot, context, target_user_id, data, requested_pin)
            if should_remove:
                remove_user(target_user_id)
            await bot.send_message(
                chat_id=admin_chat_id,
                text=(
                    "âœ… Envio finalizado.\n\n"
                    f"ðŸ“¤ <b>Enviadas:</b> <code>{1 if ok else 0}</code>\n"
                    f"âŒ <b>Falhas:</b> <code>{0 if ok else 1}</code>"
                ),
                parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_to_message_id,
            )
            return

        users = get_all_users()
        if not users:
            await bot.send_message(chat_id=admin_chat_id, text="NÃ£o hÃ¡ usuÃ¡rios salvos ainda.", reply_to_message_id=reply_to_message_id)
            return

        total = len(users)
        should_pin = requested_pin and total <= PIN_MAX_USERS
        pin_warning = ""
        if requested_pin and not should_pin:
            pin_warning = f"\nðŸ“Œ <b>Fixar foi desativado automaticamente</b> para listas acima de <code>{PIN_MAX_USERS}</code> usuÃ¡rios."
        status_msg = await bot.send_message(
            chat_id=admin_chat_id,
            text=f"ðŸš€ Iniciando transmissÃ£o...\n\nðŸ‘¥ <b>Total alvo:</b> <code>{total}</code>{pin_warning}",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to_message_id,
            reply_markup=_running_keyboard(_broadcast_control(context)),
        )

        queue: asyncio.Queue[int | None] = asyncio.Queue()
        counters: dict[str, Any] = {
            "sent": 0,
            "failed": 0,
            "processed": 0,
            "remove_ids": set(),
            "last_status_at": time.monotonic(),
            "status_lock": asyncio.Lock(),
        }

        for user_id in users:
            await queue.put(int(user_id))
        for _ in range(SEND_WORKERS):
            await queue.put(None)

        workers = [
            asyncio.create_task(
                _broadcast_worker(
                    queue,
                    context=context,
                    bot=bot,
                    data=data,
                    should_pin=should_pin,
                    counters=counters,
                    status_msg=status_msg,
                    total=total,
                )
            )
            for _ in range(SEND_WORKERS)
        ]

        await queue.join()
        await asyncio.gather(*workers, return_exceptions=True)

        removed = 0
        for user_id in counters["remove_ids"]:
            try:
                remove_user(int(user_id))
                removed += 1
            except Exception:
                LOGGER.debug("Falha ao remover usuÃ¡rio invÃ¡lido do broadcast", exc_info=True)

        cancelled = bool(_broadcast_control(context).get("cancelled"))
        final_title = "ðŸ›‘ <b>TransmissÃ£o cancelada.</b>" if cancelled else "âœ… <b>TransmissÃ£o finalizada.</b>"
        final_text = (
            f"{final_title}\n\n"
            f"ðŸ“¤ <b>Enviadas:</b> <code>{int(counters['sent'])}</code>\n"
            f"âŒ <b>Falhas:</b> <code>{int(counters['failed'])}</code>\n"
            f"ðŸ§¹ <b>Removidos:</b> <code>{removed}</code>\n"
            f"ðŸ‘¥ <b>Total processado:</b> <code>{int(counters['processed'])}</code>"
        )
        try:
            await status_msg.edit_text(final_text, parse_mode=ParseMode.HTML)
        except Exception:
            await bot.send_message(chat_id=admin_chat_id, text=final_text, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to_message_id)
    finally:
        application.bot_data[GLOBAL_BROADCAST_RUNNING_KEY] = False
        application.bot_data.pop(GLOBAL_BROADCAST_TASK_KEY, None)
        application.bot_data.pop(GLOBAL_BROADCAST_CONTROL_KEY, None)


async def _scheduled_broadcast_runner(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    application,
    bot,
    admin_chat_id: int,
    reply_to_message_id: int | None,
    data: dict[str, object],
    when_ts: float,
) -> None:
    await asyncio.sleep(max(0.0, when_ts - time.time()))
    _set_broadcast_running(context, True)
    context.application.bot_data[GLOBAL_BROADCAST_CONTROL_KEY] = {"paused": False, "cancelled": False}
    await _execute_broadcast_background(
        context=context,
        application=application,
        bot=bot,
        admin_chat_id=admin_chat_id,
        reply_to_message_id=reply_to_message_id,
        data=data,
    )


async def _start_send(update: Update, context: ContextTypes.DEFAULT_TYPE, query_message: Message, data: dict[str, object]) -> None:
    if _broadcast_is_running(context):
        await update.callback_query.answer("JÃ¡ existe uma transmissÃ£o em andamento.", show_alert=True)
        return

    mode = str(data.get("mode") or "")
    if mode not in {"all", "single"}:
        await update.callback_query.answer("Defina o destino primeiro.", show_alert=True)
        return
    if not _content_ready(data):
        await update.callback_query.answer("Defina uma mensagem ou mÃ­dia antes de enviar.", show_alert=True)
        return

    total = 1 if mode == "single" else get_total_users()
    if bool(data.get("pin")) and total > PIN_MAX_USERS:
        data["pin"] = False
    if bool(data.get("pin")) and total > PIN_WARN_USERS and not bool(data.get("confirm_pin")):
        data["confirm_pin"] = True
        await _render_panel_text(
            context,
            "ðŸ“Œ <b>Fixar mensagem exige cuidado</b>\n\n"
            f"<blockquote>VocÃª estÃ¡ tentando fixar para <code>{total}</code> usuÃ¡rios. Isso pode gerar incÃ´modo e limitaÃ§Ãµes. Confirma mesmo assim?</blockquote>",
            _confirm_keyboard(),
            query_message=query_message,
        )
        return

    schedule_at = data.get("schedule_at")
    safe_payload = dict(data)
    _set_state(context, "")
    safe_payload["confirm_pin"] = False
    data["confirm_pin"] = False

    if schedule_at and float(schedule_at) > time.time():
        task = context.application.create_task(
            _scheduled_broadcast_runner(
                context=context,
                application=context.application,
                bot=context.bot,
                admin_chat_id=int(update.effective_chat.id),
                reply_to_message_id=int(query_message.message_id),
                data=safe_payload,
                when_ts=float(schedule_at),
            )
        )
        _set_broadcast_task(context, task)
        await _show_main_menu(context, query_message=query_message, note=f"TransmissÃ£o agendada para <code>{_escape(_schedule_label(data))}</code>.")
        return

    context.application.bot_data[GLOBAL_BROADCAST_CONTROL_KEY] = {"paused": False, "cancelled": False}
    task = context.application.create_task(
        _execute_broadcast_background(
            context=context,
            application=context.application,
            bot=context.bot,
            admin_chat_id=int(update.effective_chat.id),
            reply_to_message_id=int(query_message.message_id),
            data=safe_payload,
        )
    )
    _set_broadcast_task(context, task)
    _set_broadcast_running(context, True)
    await _show_main_menu(context, query_message=query_message, note="Broadcast iniciado. Acompanhe pelo chat.")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not _is_admin(user.id):
        return

    _set_state(context, "")
    _panel_data(context)
    await _show_main_menu(context, source_message=message)
    await _delete_message_safely(message)


async def broadcast_public_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    parts = str(query.data or "").split("|", 2)
    if len(parts) == 3 and parts[1] == "alert":
        await query.answer(_public_alerts(context).get(parts[2], "Aviso indisponÃ­vel."), show_alert=True)
        return
    await query.answer()


async def broadcast_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not query.message or not user or not _is_admin(user.id):
        return

    try:
        await query.answer()
    except Exception:
        LOGGER.debug("Falha ao responder callback do broadcast", exc_info=True)

    guard = await _guard_action(context)
    if guard != "ok":
        return

    try:
        data = _panel_data(context)
        raw_action = str(query.data or "")
        if not raw_action.startswith("bc|"):
            return
        parts = raw_action.split("|")
        action = parts[1] if len(parts) > 1 else ""

        if action == "pause":
            _broadcast_control(context)["paused"] = True
            await query.answer("TransmissÃ£o pausada.")
            return
        if action == "resume":
            _broadcast_control(context)["paused"] = False
            await query.answer("TransmissÃ£o retomada.")
            return
        if action == "cancel_running":
            _broadcast_control(context)["cancelled"] = True
            _broadcast_control(context)["paused"] = False
            await query.answer("Cancelamento solicitado.")
            return

        if action == "menu":
            await _show_main_menu(context, query_message=query.message)
            return

        if action == "close":
            _reset_broadcast(context)
            await _delete_message_safely(query.message)
            return

        if action == "reset":
            context.user_data[BROADCAST_DATA_KEY] = _blank_data()
            await _show_main_menu(context, query_message=query.message)
            return

        if action == "set_mode":
            _set_state(context, "")
            await _render_panel_text(
                context,
                "ðŸŽ¯ <b>Escolha o destino</b>\n\n<i>Toque em uma das opÃ§Ãµes abaixo.</i>",
                _mode_keyboard(),
                query_message=query.message,
            )
            return

        if action == "mode_all":
            data["mode"] = "all"
            data["target_user_id"] = None
            await _show_main_menu(context, query_message=query.message, note="Destino definido para todos os usuÃ¡rios.")
            return

        if action == "mode_single":
            data["mode"] = "single"
            _set_state(context, "awaiting_target_user_id")
            await _show_prompt(
                context,
                "ðŸ‘¤ <b>Envie o ID do usuÃ¡rio</b>\n\n<i>O prÃ³ximo texto enviado serÃ¡ usado como destino da transmissÃ£o.</i>",
                query_message=query.message,
            )
            return

        if action == "set_media":
            _set_state(context, "awaiting_media")
            await _show_prompt(
                context,
                "ðŸ–¼ <b>Envie a mÃ­dia da publicaÃ§Ã£o!</b>\n"
                "<i>Tipos permitidos: fotos, vÃ­deos, arquivos, figurinhas, GIFs, Ã¡udio, mensagens de voz e vÃ­deos redondos.</i>",
                query_message=query.message,
                remove_callback="bc|remove_media",
            )
            return

        if action == "set_text":
            _set_state(context, "awaiting_text")
            await _show_prompt(
                context,
                "ðŸ“ <b>Envie a mensagem da postagem</b>\n"
                "<i>VocÃª pode usar negrito, itÃ¡lico e links feitos pelo prÃ³prio Telegram. HTML simples tambÃ©m funciona.</i>\n\n"
                "â€¢ <b>Nome:</b> <code>%firstname%</code>\n"
                "â€¢ <b>UsuÃ¡rio:</b> <code>%username%</code>",
                query_message=query.message,
                remove_callback="bc|remove_text",
            )
            return

        if action == "set_buttons":
            _set_state(context, "awaiting_buttons")
            await _show_prompt(
                context,
                "ðŸ”˜ <b>Defina os botÃµes da postagem</b>\n\n"
                "Envie uma mensagem assim:\n\n"
                "<blockquote>Texto do botÃ£o - https://t.me/Exemplo\n"
                "Outro botÃ£o - https://site.com</blockquote>\n\n"
                "Para vÃ¡rios botÃµes na mesma linha:\n"
                "<blockquote>Site - https://site.com && Canal - https://t.me/canal</blockquote>\n\n"
                "TambÃ©m aceita:\n"
                "<blockquote>Aviso - popup:Texto do popup\n"
                "Compartilhar - share:Texto para compartilhar</blockquote>",
                query_message=query.message,
                remove_callback="bc|remove_buttons",
            )
            return

        if action == "schedule":
            _set_state(context, "awaiting_schedule")
            await _show_prompt(
                context,
                "ðŸ—“ <b>Agendar transmissÃ£o</b>\n\n"
                "<i>Envie o horÃ¡rio desejado.</i>\n\n"
                "Exemplos:\n"
                "<blockquote>20:00\nhoje 20:00\namanhÃ£ 12:00\n25/12/2026 08:30</blockquote>",
                query_message=query.message,
                remove_callback="bc|remove_schedule",
            )
            return

        if action == "save_template":
            if not _content_ready(data):
                await query.answer("Defina texto ou mÃ­dia antes de salvar.", show_alert=True)
                return
            _set_state(context, "awaiting_template_name")
            await _show_prompt(
                context,
                "ðŸ’¾ <b>Salvar modelo</b>\n\n<i>Envie um nome curto para este modelo.</i>",
                query_message=query.message,
            )
            return

        if action == "use_template":
            templates = _load_templates()
            if not templates:
                await _show_main_menu(context, query_message=query.message, note="Nenhum modelo salvo ainda.")
                return
            await _render_panel_text(
                context,
                "ðŸ“š <b>Modelos salvos</b>\n\n<i>Escolha um modelo para carregar no painel.</i>",
                _templates_keyboard(templates),
                query_message=query.message,
            )
            return

        if action == "load_template" and len(parts) >= 3:
            templates = _load_templates()
            try:
                template = templates[int(parts[2])]
            except Exception:
                await query.answer("Modelo nÃ£o encontrado.", show_alert=True)
                return
            _apply_template(data, template)
            await _show_main_menu(context, query_message=query.message, note=f"Modelo carregado: <code>{_escape(template.get('name'))}</code>.")
            return

        if action == "remove_media":
            data["source_chat_id"] = None
            data["source_message_id"] = None
            data["media_type"] = None
            data["media_file_id"] = None
            data["has_media"] = False
            await _show_main_menu(context, query_message=query.message, note="MÃ­dia removida.")
            return

        if action == "remove_text":
            data["text"] = ""
            await _show_main_menu(context, query_message=query.message, note="Texto removido.")
            return

        if action == "remove_buttons":
            data["button_rows"] = []
            await _show_main_menu(context, query_message=query.message, note="BotÃµes removidos.")
            return

        if action == "remove_schedule":
            data["schedule_at"] = None
            await _show_main_menu(context, query_message=query.message, note="Agendamento removido.")
            return

        if action == "toggle_pin":
            data["pin"] = not bool(data.get("pin"))
            data["confirm_pin"] = False
            note = "Fixar ativado. HaverÃ¡ trava automÃ¡tica para listas grandes." if bool(data.get("pin")) else "Fixar desativado."
            await _show_main_menu(context, query_message=query.message, note=note)
            return

        if action == "preview":
            await _show_preview(context, query_message=query.message)
            return

        if action == "test_send":
            ok, note = await _send_test(context, int(user.id), data)
            await _show_main_menu(context, query_message=query.message, note=note)
            return

        if action == "send":
            total = 1 if data.get("mode") == "single" else get_total_users()
            await _render_panel_text(
                context,
                "ðŸ“¬ <b>TransmissÃ£o</b>\n\n"
                f"VocÃª tem certeza que quer enviar para <code>{total}</code> usuÃ¡rio(s)?\n\n"
                f"<blockquote>MÃ­dia: <b>{_yes_no(bool(data.get('has_media')))}</b>\n"
                f"BotÃµes: <b>{_button_count(data)}</b>\n"
                f"Fixar: <b>{_yes_no(bool(data.get('pin')))}</b>\n"
                f"Agendado: <b>{_escape(_schedule_label(data))}</b></blockquote>",
                _confirm_keyboard(),
                query_message=query.message,
            )
            return

        if action == "confirm_send":
            await _start_send(update, context, query.message, data)
            return
    finally:
        _release_guard(context)


async def broadcast_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not _is_admin(user.id):
        return

    state = _get_state(context)
    if not state:
        return

    data = _panel_data(context)

    try:
        if state == "awaiting_target_user_id":
            raw = str(message.text or "").strip()
            if not raw.isdigit():
                await _show_prompt(
                    context,
                    "ðŸ‘¤ <b>Envie o ID do usuÃ¡rio</b>\n\n<i>O prÃ³ximo texto enviado serÃ¡ usado como destino da transmissÃ£o.</i>",
                    source_message=message,
                    note="Envie um ID numÃ©rico vÃ¡lido.",
                )
                return

            data["mode"] = "single"
            data["target_user_id"] = int(raw)
            await _show_main_menu(context, source_message=message, note=f"UsuÃ¡rio alvo salvo: <code>{_escape(raw)}</code>.")
            return

        if state == "awaiting_media":
            if not (
                message.photo
                or message.video
                or message.document
                or message.animation
                or message.audio
                or message.voice
                or message.video_note
                or message.sticker
            ):
                await _show_prompt(
                    context,
                    "ðŸ–¼ <b>Envie a mÃ­dia da publicaÃ§Ã£o!</b>\n"
                    "<i>Tipos permitidos: fotos, vÃ­deos, arquivos, figurinhas, GIFs, Ã¡udio, mensagens de voz e vÃ­deos redondos.</i>",
                    source_message=message,
                    remove_callback="bc|remove_media",
                    note="Envie uma mÃ­dia vÃ¡lida para continuar.",
                )
                return

            media_type = None
            media_file_id = None
            if message.photo:
                media_type = "photo"
                media_file_id = message.photo[-1].file_id
            elif message.video:
                media_type = "video"
                media_file_id = message.video.file_id
            elif message.document:
                media_type = "document"
                media_file_id = message.document.file_id
            elif message.animation:
                media_type = "animation"
                media_file_id = message.animation.file_id
            elif message.audio:
                media_type = "audio"
                media_file_id = message.audio.file_id
            elif message.voice:
                media_type = "voice"
                media_file_id = message.voice.file_id
            elif message.video_note:
                media_type = "video_note"
                media_file_id = message.video_note.file_id
            elif message.sticker:
                media_type = "sticker"
                media_file_id = message.sticker.file_id

            data["source_chat_id"] = int(message.chat_id)
            data["source_message_id"] = int(message.message_id)
            data["media_type"] = media_type
            data["media_file_id"] = media_file_id
            data["has_media"] = True
            await _show_main_menu(context, source_message=message, note="MÃ­dia salva com sucesso.")
            return

        if state == "awaiting_text":
            raw = _message_text_from(message)
            if not raw:
                await _show_prompt(
                    context,
                    "ðŸ“ <b>Envie a mensagem da postagem</b>\n"
                    "<i>VocÃª pode usar negrito, itÃ¡lico e links feitos pelo prÃ³prio Telegram. HTML simples tambÃ©m funciona.</i>",
                    source_message=message,
                    remove_callback="bc|remove_text",
                    note="Envie um texto para continuar.",
                )
                return

            data["text"] = raw
            await _show_main_menu(context, source_message=message, note="Mensagem salva com formataÃ§Ã£o.")
            return

        if state == "awaiting_buttons":
            rows, error = _parse_buttons(str(message.text or ""))
            if error:
                await _show_prompt(
                    context,
                    "ðŸ”˜ <b>Defina os botÃµes da postagem</b>\n\n"
                    "<blockquote>Texto do botÃ£o - https://t.me/Exemplo\n"
                    "Linha com dois - https://site.com && Canal - https://t.me/canal</blockquote>",
                    source_message=message,
                    remove_callback="bc|remove_buttons",
                    note=error,
                )
                return
            data["button_rows"] = rows
            await _show_main_menu(context, source_message=message, note=f"{_button_count(data)} botÃ£o(Ãµes) salvo(s).")
            return

        if state == "awaiting_schedule":
            when_ts = _parse_when(str(message.text or ""))
            if not when_ts:
                await _show_prompt(
                    context,
                    "ðŸ—“ <b>Agendar transmissÃ£o</b>\n\n"
                    "<blockquote>20:00\nhoje 20:00\namanhÃ£ 12:00\n25/12/2026 08:30</blockquote>",
                    source_message=message,
                    remove_callback="bc|remove_schedule",
                    note="NÃ£o entendi esse horÃ¡rio. Envie uma data futura.",
                )
                return
            data["schedule_at"] = when_ts
            await _show_main_menu(context, source_message=message, note=f"Agendado para <code>{_escape(_schedule_label(data))}</code>.")
            return

        if state == "awaiting_template_name":
            name = str(message.text or "").strip()[:40]
            if not name:
                await _show_prompt(context, "ðŸ’¾ <b>Salvar modelo</b>\n\n<i>Envie um nome curto para este modelo.</i>", source_message=message, note="Nome invÃ¡lido.")
                return
            templates = _load_templates()
            templates.insert(0, _template_payload(data, name))
            _save_templates(templates)
            await _show_main_menu(context, source_message=message, note=f"Modelo salvo: <code>{_escape(name)}</code>.")
            return
    finally:
        await _delete_message_safely(message)
