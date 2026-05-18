from __future__ import annotations

import asyncio
import html
from dataclasses import dataclass

from telegram.error import TimedOut

from config import EPUB_QUEUE_LIMIT, EPUB_WORKERS, PDF_PROTECT_CONTENT
from core.document_archive import (
    archive_document,
    copy_archived_document,
    forget_archived_document,
    get_archived_document,
)
from services.epub_service import get_or_build_epub


@dataclass
class EpubJob:
    chat_id: int
    chapter_id: str
    chapter_number: str
    title_name: str
    images: list[str]
    caption: str
    send_status: bool = True


_workers = []
_active_jobs = {}


def _html(value) -> str:
    return html.escape(str(value or ""))


async def _safe_edit(message, text: str):
    try:
        await message.edit_text(text, parse_mode="HTML")
    except Exception:
        pass


async def _send_document_safe(bot, chat_id: int, epub_path: str, epub_name: str, caption: str):
    try:
        with open(epub_path, "rb") as file:
            await bot.send_document(
                chat_id=chat_id,
                document=file,
                filename=epub_name,
                caption=caption,
                parse_mode="HTML",
                protect_content=PDF_PROTECT_CONTENT,
            )
        return True
    except TimedOut:
        try:
            await bot.send_message(chat_id, "O envio demorou mais que o esperado. Confere se o arquivo ja chegou.")
        except Exception:
            pass
        return True


async def _progress(entry, title_name: str, chapter_number: str, done: int, total: int):
    pct = int((done / max(total, 1)) * 100)
    text = (
        "<b>Gerando EPUB</b>\n\n"
        f"<b>Obra:</b> {_html(title_name)}\n"
        f"<b>Capitulo:</b> {_html(chapter_number)}\n"
        f"<b>Progresso:</b> {pct}%"
    )
    for message in list(entry["status_messages"]):
        await _safe_edit(message, text)


async def _process_job(app, job: EpubJob):
    entry = _active_jobs.get(job.chapter_id)
    if not entry:
        return

    try:
        async def progress_cb(done, total):
            await _progress(entry, job.title_name, job.chapter_number, done, total)

        epub_path, epub_name = await get_or_build_epub(
            chapter_id=job.chapter_id,
            chapter_number=job.chapter_number,
            title_name=job.title_name,
            images=job.images,
            progress_cb=progress_cb,
        )

        archive_entry = get_archived_document("epub", job.chapter_id)
        if not archive_entry:
            archive_entry = await archive_document(
                app.bot,
                kind="epub",
                chapter_id=job.chapter_id,
                title_name=job.title_name,
                chapter_number=job.chapter_number,
                file_path=epub_path,
                file_name=epub_name,
                caption=job.caption,
            )

        for waiter in entry["waiters"]:
            copied = False
            if archive_entry:
                copied = await copy_archived_document(app.bot, waiter["chat_id"], archive_entry, waiter["caption"])
            if not copied:
                await _send_document_safe(app.bot, waiter["chat_id"], epub_path, epub_name, waiter["caption"])

        for message in list(entry["status_messages"]):
            await _safe_edit(
                message,
                (
                    "<b>EPUB pronto</b>\n\n"
                    f"<b>Obra:</b> {_html(job.title_name)}\n"
                    f"<b>Capitulo:</b> {_html(job.chapter_number)}"
                ),
            )
    except Exception as error:
        for message in list(entry["status_messages"]):
            await _safe_edit(message, f"Falha ao gerar EPUB:\n<code>{html.escape(str(error))}</code>")
        if not entry["status_messages"]:
            for waiter in entry["waiters"]:
                try:
                    await app.bot.send_message(
                        waiter["chat_id"],
                        (
                            "<b>Falha ao gerar EPUB</b>\n\n"
                            f"<b>Obra:</b> {_html(job.title_name)}\n"
                            f"<b>Capitulo:</b> {_html(job.chapter_number)}\n"
                            f"<code>{html.escape(str(error))}</code>"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    finally:
        _active_jobs.pop(job.chapter_id, None)


async def _worker(app, queue):
    while True:
        job = await queue.get()
        if job is None:
            queue.task_done()
            break
        await _process_job(app, job)
        queue.task_done()


async def enqueue_epub_job(app, job: EpubJob):
    queue = app.bot_data["epub_queue"]

    archive_entry = get_archived_document("epub", job.chapter_id)
    if archive_entry:
        status = None
        if job.send_status:
            status = await app.bot.send_message(
                job.chat_id,
                (
                    "<b>EPUB encontrado no acervo</b>\n\n"
                    f"<b>Obra:</b> {_html(job.title_name)}\n"
                    f"<b>Capitulo:</b> {_html(job.chapter_number)}"
                ),
                parse_mode="HTML",
            )
        copied = await copy_archived_document(app.bot, job.chat_id, archive_entry, job.caption)
        if copied:
            if status:
                await _safe_edit(
                    status,
                    (
                        "<b>EPUB pronto</b>\n\n"
                        f"<b>Obra:</b> {_html(job.title_name)}\n"
                        f"<b>Capitulo:</b> {_html(job.chapter_number)}"
                    ),
                )
            return queue.qsize()
        forget_archived_document("epub", job.chapter_id)

    if job.chapter_id in _active_jobs:
        entry = _active_jobs[job.chapter_id]
        entry["waiters"].append({"chat_id": job.chat_id, "caption": job.caption})
        if job.send_status:
            status = await app.bot.send_message(
                job.chat_id,
                (
                    "<b>Pedido recebido</b>\n\n"
                    f"<b>Obra:</b> {_html(job.title_name)}\n"
                    f"<b>Capitulo:</b> {_html(job.chapter_number)}\n"
                    "Status: <b>ja esta em processamento</b>"
                ),
                parse_mode="HTML",
            )
            entry["status_messages"].append(status)
        return queue.qsize()

    status_messages = []
    if job.send_status:
        status = await app.bot.send_message(
            job.chat_id,
            (
                "<b>Pedido recebido</b>\n\n"
                f"<b>Obra:</b> {_html(job.title_name)}\n"
                f"<b>Capitulo:</b> {_html(job.chapter_number)}\n"
                "Formato: <b>EPUB</b>\n"
                "Status: <b>na fila</b>"
            ),
            parse_mode="HTML",
        )
        status_messages.append(status)

    _active_jobs[job.chapter_id] = {
        "waiters": [{"chat_id": job.chat_id, "caption": job.caption}],
        "status_messages": status_messages,
    }

    try:
        await queue.put(job)
    except BaseException:
        _active_jobs.pop(job.chapter_id, None)
        raise
    return queue.qsize()


async def start_epub_workers(app):
    if app.bot_data.get("epub_workers_started"):
        return

    app.bot_data["epub_queue"] = asyncio.Queue(maxsize=EPUB_QUEUE_LIMIT)
    for _ in range(EPUB_WORKERS):
        _workers.append(asyncio.create_task(_worker(app, app.bot_data["epub_queue"])))
    app.bot_data["epub_workers_started"] = True


async def stop_epub_workers(app):
    queue = app.bot_data.get("epub_queue")
    if queue is None:
        return
    for _ in _workers:
        await queue.put(None)
    await asyncio.gather(*_workers, return_exceptions=True)
    _workers.clear()
