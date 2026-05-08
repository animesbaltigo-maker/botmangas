from __future__ import annotations

import asyncio
import base64
import io
import os
import time
from typing import Any

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut, NetworkError

from config import ADMIN_IDS
from services.control_blocklist import block_user, get_blocked_users, is_blocked
from services.user_registry import get_all_users, get_total_users, remove_user

CONTROL_SECRET = os.getenv("CONTROL_SECRET", "")
CONTROL_AGENT_ENABLED = os.getenv("CONTROL_AGENT_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
CONTROL_AGENT_HOST = os.getenv("CONTROL_AGENT_HOST", "127.0.0.1")
CONTROL_AGENT_PORT = int(os.getenv("CONTROL_AGENT_PORT", "8787"))
CONTROL_BOT_ID = os.getenv("CONTROL_BOT_ID", os.getenv("BOT_USERNAME", "bot")).strip() or "bot"
CONTROL_BOT_NAME = os.getenv("CONTROL_BOT_NAME", CONTROL_BOT_ID)

_RUNNER: web.AppRunner | None = None
_SITE: web.TCPSite | None = None
_STATE: dict[str, Any] = {"broadcast_running": False, "started_at": int(time.time())}


def _authorized(request: web.Request) -> bool:
    return bool(CONTROL_SECRET) and request.headers.get("X-Control-Secret") == CONTROL_SECRET


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    if request.path.startswith("/control/") and not _authorized(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    return await handler(request)


def _button_markup(rows: list[list[dict[str, str]]] | None) -> InlineKeyboardMarkup | None:
    built: list[list[InlineKeyboardButton]] = []
    for row in rows or []:
        line: list[InlineKeyboardButton] = []
        for button in row:
            text = str(button.get("text") or "Bot?o")[:64]
            value = str(button.get("value") or button.get("url") or "")
            kind = str(button.get("type") or "url")
            if kind == "alert":
                # Bot?es popup globais precisam ser resolvidos pelo broadcast nativo; na central viram link ignorado.
                continue
            if value.startswith("t.me/"):
                value = "https://" + value
            if value.startswith(("http://", "https://", "tg://")):
                line.append(InlineKeyboardButton(text, url=value))
        if line:
            built.append(line)
    return InlineKeyboardMarkup(built) if built else None


def _media_bytes(payload: dict[str, Any]) -> tuple[str, bytes, str] | None:
    media = payload.get("media")
    if not isinstance(media, dict):
        return None
    encoded = str(media.get("data") or "")
    if not encoded:
        return None
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return None
    media_type = str(media.get("type") or "document")
    filename = str(media.get("filename") or "broadcast.bin")
    return media_type, raw, filename


def _fresh_file(raw: bytes, filename: str) -> io.BytesIO:
    file_obj = io.BytesIO(raw)
    file_obj.name = filename
    return file_obj


async def _send_one(bot, user_id: int, payload: dict[str, Any]) -> tuple[bool, bool]:
    text = str(payload.get("text") or "").strip()
    buttons = _button_markup(payload.get("button_rows") if isinstance(payload.get("button_rows"), list) else [])
    pin = bool(payload.get("pin"))
    media = _media_bytes(payload)
    try:
        if media:
            media_type, raw, filename = media
            file_obj = _fresh_file(raw, filename)
            if media_type == "photo":
                sent = await bot.send_photo(user_id, photo=file_obj, caption=text or None, parse_mode=ParseMode.HTML, reply_markup=buttons)
            elif media_type == "video":
                sent = await bot.send_video(user_id, video=file_obj, caption=text or None, parse_mode=ParseMode.HTML, reply_markup=buttons)
            elif media_type == "animation":
                sent = await bot.send_animation(user_id, animation=file_obj, caption=text or None, parse_mode=ParseMode.HTML, reply_markup=buttons)
            elif media_type == "audio":
                sent = await bot.send_audio(user_id, audio=file_obj, caption=text or None, parse_mode=ParseMode.HTML, reply_markup=buttons)
            else:
                sent = await bot.send_document(user_id, document=file_obj, caption=text or None, parse_mode=ParseMode.HTML, reply_markup=buttons)
        else:
            sent = await bot.send_message(user_id, text or "??", parse_mode=ParseMode.HTML, reply_markup=buttons, disable_web_page_preview=True)
        if pin and sent:
            try:
                await bot.pin_chat_message(user_id, sent.message_id, disable_notification=True)
            except Exception:
                pass
        return True, False
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 1)
        return await _send_one(bot, user_id, {**payload, "pin": False})
    except (Forbidden, BadRequest) as exc:
        lowered = str(exc).lower()
        return False, any(part in lowered for part in ("blocked", "deactivated", "chat not found", "forbidden"))
    except (TimedOut, NetworkError):
        return False, False
    except Exception:
        return False, False


async def _broadcast_task(app, payload: dict[str, Any]) -> None:
    _STATE["broadcast_running"] = True
    counters = {"sent": 0, "failed": 0, "removed": 0, "total": 0, "started_at": int(time.time())}
    _STATE["last_broadcast"] = counters
    try:
        users = [uid for uid in get_all_users() if not is_blocked(uid)]
        counters["total"] = len(users)
        for user_id in users:
            ok, should_remove = await _send_one(app.bot, int(user_id), payload)
            if ok:
                counters["sent"] += 1
            else:
                counters["failed"] += 1
                if should_remove:
                    remove_user(int(user_id))
                    counters["removed"] += 1
            await asyncio.sleep(0.12)
    finally:
        counters["finished_at"] = int(time.time())
        _STATE["broadcast_running"] = False


async def _health(request: web.Request) -> web.Response:
    app = request.app["telegram_app"]
    try:
        me = await app.bot.get_me()
        username = me.username or CONTROL_BOT_NAME
    except Exception:
        username = CONTROL_BOT_NAME
    return web.json_response({
        "ok": True,
        "bot_id": CONTROL_BOT_ID,
        "name": CONTROL_BOT_NAME,
        "username": username,
        "online": True,
        "uptime_seconds": int(time.time()) - int(_STATE.get("started_at", time.time())),
        "broadcast_running": bool(_STATE.get("broadcast_running")),
    })


async def _metrics(request: web.Request) -> web.Response:
    blocked = get_blocked_users()
    return web.json_response({
        "ok": True,
        "bot_id": CONTROL_BOT_ID,
        "name": CONTROL_BOT_NAME,
        "users_active": get_total_users(),
        "users_inactive": 0,
        "users_banned": len(blocked),
        "admins": len(ADMIN_IDS),
        "broadcast_running": bool(_STATE.get("broadcast_running")),
        "last_broadcast": _STATE.get("last_broadcast") or {},
    })


async def _block(request: web.Request) -> web.Response:
    payload = await request.json()
    user_id = int(payload.get("user_id") or 0)
    if user_id <= 0:
        return web.json_response({"ok": False, "error": "invalid_user_id"}, status=400)
    block_user(user_id, username=str(payload.get("username") or ""), reason=str(payload.get("reason") or ""), actor_id=int(payload.get("actor_id") or 0) or None)
    try:
        remove_user(user_id)
    except Exception:
        pass
    return web.json_response({"ok": True, "user_id": user_id, "blocked": True})


async def _broadcast(request: web.Request) -> web.Response:
    app = request.app["telegram_app"]
    payload = await request.json()
    if bool(_STATE.get("broadcast_running")):
        return web.json_response({"ok": False, "error": "broadcast_running"}, status=409)
    if not str(payload.get("text") or "").strip() and not isinstance(payload.get("media"), dict):
        return web.json_response({"ok": False, "error": "empty_broadcast"}, status=400)
    task = app.create_task(_broadcast_task(app, payload))
    _STATE["broadcast_task"] = task
    return web.json_response({"ok": True, "started": True, "target_users": get_total_users()})


async def start_control_agent(app) -> None:
    global _RUNNER, _SITE
    if not CONTROL_AGENT_ENABLED:
        return
    if not CONTROL_SECRET:
        raise RuntimeError("CONTROL_SECRET precisa estar configurado para ativar o control agent.")
    web_app = web.Application(middlewares=[_auth_middleware])
    web_app["telegram_app"] = app
    web_app.router.add_get("/control/health", _health)
    web_app.router.add_get("/control/metrics", _metrics)
    web_app.router.add_post("/control/block", _block)
    web_app.router.add_post("/control/broadcast", _broadcast)
    _RUNNER = web.AppRunner(web_app)
    await _RUNNER.setup()
    _SITE = web.TCPSite(_RUNNER, CONTROL_AGENT_HOST, CONTROL_AGENT_PORT)
    await _SITE.start()


async def stop_control_agent(app=None) -> None:
    global _RUNNER, _SITE
    if _SITE is not None:
        await _SITE.stop()
        _SITE = None
    if _RUNNER is not None:
        await _RUNNER.cleanup()
        _RUNNER = None
