import asyncio
import json
import traceback
from datetime import datetime, timezone
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from config import BOT_TOKEN, CACHE_CLEANUP_INTERVAL_SECONDS, CACHE_CLEANUP_STARTUP
from core.epub_queue import start_epub_workers, stop_epub_workers
from core.http_client import close_http_client
from core.pdf_queue import start_pdf_workers, stop_pdf_workers
from handlers.broadcast import (
    broadcast_callbacks,
    broadcast_command,
    broadcast_message_router,
    broadcast_public_callbacks,
)
from handlers.callbacks import callbacks
from handlers.help import ajuda
from handlers.language import idioma
from handlers.inline import chosen_inline_result, inline_query
from handlers.metricas import metricas, metricas_limpar
from handlers.novoseps import auto_post_new_eps_job, postnovoseps
from handlers.offline_admin import liberar, offlineadd, offlinecheck, offlinerevoke
from handlers.pdf_bulk import pdfmanga
from handlers.plan import plano
from handlers.referral import indicacoes, referral_button
from handlers.referral_admin import auto_referral_check_job, refstats
from handlers.profile import mperfil
from handlers.search import buscar, buscar_texto_livre
from handlers.start import start
from services.i18n import t_user
from services.catalog_client import schedule_warm_catalog_cache, warm_catalog_cache
from services.cache_cleanup import cleanup_cache_once
from services.metrics import init_metrics_db
from services.offline_access import init_offline_access_db
from services.referral_db import init_referral_db
from services.affiliate_db import init_affiliate_db, release_due_commissions
from handlers.postmanga import postmanga, postallmangas

init_metrics_db()
init_referral_db()
init_offline_access_db()
init_affiliate_db()

MAX_CONCURRENT_UPDATES = 128
BOT_API_CONNECTION_POOL = 64
BOT_API_POOL_TIMEOUT = 30.0
BOT_API_CONNECT_TIMEOUT = 10.0
BOT_API_READ_TIMEOUT = 25.0
BOT_API_WRITE_TIMEOUT = 25.0


def _bot_log(event: str, **payload: Any) -> None:
    data = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": event,
        **payload,
    }
    try:
        print("[BOT_DEBUG]", json.dumps(data, ensure_ascii=True, default=str), flush=True)
    except Exception:
        print("[BOT_DEBUG_FALLBACK]", event, repr(payload), flush=True)


async def post_init(app: Application) -> None:
    await start_pdf_workers(app)
    await start_epub_workers(app)
    await set_bot_commands_job(app)
    schedule_warm_catalog_cache()
    if CACHE_CLEANUP_STARTUP:
        asyncio.create_task(cleanup_cache_once())
    try:
        me = await app.bot.get_me()
        webhook = await app.bot.get_webhook_info()
        _bot_log(
            "post_init_bot_status",
            bot_id=me.id,
            username=me.username,
            can_join_groups=me.can_join_groups,
            can_read_all_group_messages=me.can_read_all_group_messages,
            supports_inline_queries=me.supports_inline_queries,
            webhook_url_set=bool(webhook.url),
            webhook_url=webhook.url,
            pending_update_count=webhook.pending_update_count,
            last_error_date=webhook.last_error_date,
            last_error_message=webhook.last_error_message,
        )
    except Exception as error:
        _bot_log(
            "post_init_status_error",
            error_type=type(error).__name__,
            error_repr=repr(error),
            traceback=traceback.format_exc(),
        )


async def post_shutdown(app: Application) -> None:
    await stop_epub_workers(app)
    await stop_pdf_workers(app)
    await close_http_client()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    _bot_log(
        "application_error",
        error_type=type(context.error).__name__ if context.error else "",
        error_repr=repr(context.error),
        traceback="".join(traceback.format_exception(None, context.error, context.error.__traceback__)) if context.error else "",
        update=update.to_dict() if isinstance(update, Update) else repr(update),
    )
    try:
        if isinstance(update, Update) and update.effective_message:
            user_id = getattr(update.effective_user, "id", None)
            await update.effective_message.reply_text(t_user(user_id, "common.generic_error"))
    except Exception:
        pass


async def update_probe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.inline_query:
        query = update.inline_query
        _bot_log(
            "probe_inline_update",
            update_id=update.update_id,
            inline_query_id=query.id,
            from_user=query.from_user.to_dict() if query.from_user else None,
            query=query.query,
            offset=query.offset,
            chat_type=query.chat_type,
            full_update=update.to_dict(),
        )
    elif update.chosen_inline_result:
        chosen = update.chosen_inline_result
        _bot_log(
            "probe_chosen_inline_update",
            update_id=update.update_id,
            result_id=chosen.result_id,
            from_user=chosen.from_user.to_dict() if chosen.from_user else None,
            query=chosen.query,
            inline_message_id=chosen.inline_message_id,
            full_update=update.to_dict(),
        )


async def warm_catalog_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await warm_catalog_cache()


async def cache_cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await cleanup_cache_once()


async def affiliate_release_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        release_due_commissions()
    except Exception as error:
        print("ERRO AFFILIATE RELEASE:", repr(error))


async def set_bot_commands_job(app: Application) -> None:
    try:
        await app.bot.set_my_commands(
            [
                ("start", "Iniciar o bot"),
                ("buscar", "Buscar mangá, manhwa ou manhua"),
                ("idioma", "Alterar idioma do bot"),
                ("ajuda", "Ver como usar"),
                ("plano", "Ver planos e status"),
                ("indicacoes", "Ver link e ganhos de indicação"),
                ("liberar", "Liberar offline manualmente"),
            ]
        )
    except Exception as error:
        _bot_log("set_commands_error", error_type=type(error).__name__, error_repr=repr(error))


def _register_jobs(app: Application) -> None:
    if not app.job_queue:
        print("JobQueue nao disponivel. Instale python-telegram-bot[job-queue]==22.6")
        return

    app.job_queue.run_repeating(
        auto_post_new_eps_job,
        interval=600,
        first=20,
        name="auto_post_new_chapters",
    )
    app.job_queue.run_repeating(
        auto_referral_check_job,
        interval=3600,
        first=60,
        name="auto_referral_check",
    )
    app.job_queue.run_repeating(
        warm_catalog_job,
        interval=600,
        first=5,
        name="warm_catalog_cache",
    )
    app.job_queue.run_repeating(
        cache_cleanup_job,
        interval=max(300, CACHE_CLEANUP_INTERVAL_SECONDS),
        first=120,
        name="cache_cleanup",
    )
    app.job_queue.run_repeating(
        affiliate_release_job,
        interval=1800,
        first=90,
        name="affiliate_release",
    )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Configure BOT_TOKEN nas variaveis de ambiente.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(MAX_CONCURRENT_UPDATES)
        .connection_pool_size(BOT_API_CONNECTION_POOL)
        .pool_timeout(BOT_API_POOL_TIMEOUT)
        .connect_timeout(BOT_API_CONNECT_TIMEOUT)
        .read_timeout(BOT_API_READ_TIMEOUT)
        .write_timeout(BOT_API_WRITE_TIMEOUT)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buscar", buscar))
    app.add_handler(CommandHandler("idioma", idioma))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CommandHandler("postnovoseps", postnovoseps))
    app.add_handler(CommandHandler("postnovoscaps", postnovoseps))
    app.add_handler(CommandHandler("postmanga", postmanga))
    app.add_handler(CommandHandler("pdfmanga", pdfmanga))
    app.add_handler(CommandHandler("pdfall", pdfmanga))
    app.add_handler(CommandHandler("plano", plano))
    app.add_handler(CommandHandler("offlineadd", offlineadd))
    app.add_handler(CommandHandler("liberar", liberar))
    app.add_handler(CommandHandler("offlinecheck", offlinecheck))
    app.add_handler(CommandHandler("offlinerevoke", offlinerevoke))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("indicacoes", indicacoes))
    app.add_handler(CommandHandler("refstats", refstats))
    app.add_handler(CommandHandler("metricas", metricas))
    app.add_handler(CommandHandler("metricaslimpar", metricas_limpar))
    app.add_handler(CommandHandler("mperfil", mperfil))
    app.add_handler(TypeHandler(Update, update_probe, block=False), group=-100)
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChosenInlineResultHandler(chosen_inline_result))
    app.add_handler(CommandHandler("postallmangas", postallmangas))
    app.add_handler(CommandHandler("posttodosmangas", postallmangas))

    app.add_handler(CallbackQueryHandler(broadcast_callbacks, pattern=r"^bc\|"))
    app.add_handler(CallbackQueryHandler(broadcast_public_callbacks, pattern=r"^bc_public\|"))
    app.add_handler(CallbackQueryHandler(referral_button, pattern=r"^noop_indicar$"))
    app.add_handler(CallbackQueryHandler(callbacks, pattern=r"^mb\|"))

    app.add_handler(
        MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, buscar_texto_livre),
        group=10,
    )

    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_message_router),
        group=99,
    )

    _register_jobs(app)
    app.add_error_handler(error_handler)

    allowed_updates = list(Update.ALL_TYPES)
    print("Bot de mangas rodando...", flush=True)
    _bot_log("run_polling_start", allowed_updates=allowed_updates)
    app.run_polling(drop_pending_updates=True, allowed_updates=allowed_updates)


if __name__ == "__main__":
    main()
