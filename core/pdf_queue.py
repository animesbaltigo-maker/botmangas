import asyncio
import html
from dataclasses import dataclass

from telegram.error import TimedOut

from config import PDF_PROTECT_CONTENT, PDF_QUEUE_LIMIT, PDF_WORKERS_BULK, PDF_WORKERS_SINGLE
from core.document_archive import (
    archive_document,
    copy_archived_document,
    forget_archived_document,
    get_archived_document,
)
from services.pdf_service import get_or_build_pdf


@dataclass
class PdfJob:
    chat_id: int
    chapter_id: str
    chapter_number: str
    title_name: str
    images: list[str]
    caption: str
    is_bulk: bool = False
    send_status: bool = True


_single_workers = []
_bulk_workers = []
_active_jobs = {}


def _html(value) -> str:
    return html.escape(str(value or ""))


async def _safe_edit(message, text: str):
    try:
        await message.edit_text(text, parse_mode="HTML")
    except Exception:
        pass


async def _send_document_safe(bot, chat_id: int, pdf_path: str, pdf_name: str, caption: str):
    try:
        with open(pdf_path, "rb") as file:
            await bot.send_document(
                chat_id=chat_id,
                document=file,
                filename=pdf_name,
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
        "<b>Gerando PDF</b>\n\n"
        f"<b>Obra:</b> {_html(title_name)}\n"
        f"<b>Capitulo:</b> {_html(chapter_number)}\n"
        f"<b>Progresso:</b> {pct}%"
    )
    for message in list(entry["status_messages"]):
        await _safe_edit(message, text)


async def _process_job(app, job: PdfJob):
    entry = _active_jobs.get(job.chapter_id)
    if not entry:
        return

    try:
        async def progress_cb(done, total):
            await _progress(entry, job.title_name, job.chapter_number, done, total)

        pdf_path, pdf_name = await get_or_build_pdf(
            chapter_id=job.chapter_id,
            chapter_number=job.chapter_number,
            title_name=job.title_name,
            images=job.images,
            progress_cb=progress_cb,
        )

        archive_entry = get_archived_document("pdf", job.chapter_id)
        if not archive_entry:
            archive_entry = await archive_document(
                app.bot,
                kind="pdf",
                chapter_id=job.chapter_id,
                title_name=job.title_name,
                chapter_number=job.chapter_number,
                file_path=pdf_path,
                file_name=pdf_name,
                caption=job.caption,
            )

        for waiter in entry["waiters"]:
            copied = False
            if archive_entry:
                copied = await copy_archived_document(app.bot, waiter["chat_id"], archive_entry, waiter["caption"])
            if not copied:
                await _send_document_safe(app.bot, waiter["chat_id"], pdf_path, pdf_name, waiter["caption"])

        for message in list(entry["status_messages"]):
            await _safe_edit(
                message,
                (
                    "<b>PDF pronto</b>\n\n"
                    f"<b>Obra:</b> {_html(job.title_name)}\n"
                    f"<b>Capitulo:</b> {_html(job.chapter_number)}"
                ),
            )
    except Exception as error:
        for message in list(entry["status_messages"]):
            await _safe_edit(message, f"Falha ao gerar PDF:\n<code>{html.escape(str(error))}</code>")
        if not entry["status_messages"]:
            for waiter in entry["waiters"]:
                try:
                    await app.bot.send_message(
                        waiter["chat_id"],
                        (
                            "<b>Falha ao gerar PDF</b>\n\n"
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


async def enqueue_pdf_job(app, job: PdfJob):
    single_queue = app.bot_data["single_pdf_queue"]
    bulk_queue = app.bot_data["bulk_pdf_queue"]

    archive_entry = get_archived_document("pdf", job.chapter_id)
    if archive_entry:
        status = None
        if job.send_status:
            status = await app.bot.send_message(
                job.chat_id,
                (
                    "<b>PDF encontrado no acervo</b>\n\n"
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
                        "<b>PDF pronto</b>\n\n"
                        f"<b>Obra:</b> {_html(job.title_name)}\n"
                        f"<b>Capitulo:</b> {_html(job.chapter_number)}"
                    ),
                )
            return single_queue.qsize() + bulk_queue.qsize()
        forget_archived_document("pdf", job.chapter_id)

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
        return single_queue.qsize() + bulk_queue.qsize()

    status_messages = []
    if job.send_status:
        status = await app.bot.send_message(
            job.chat_id,
            (
                "<b>Pedido recebido</b>\n\n"
                f"<b>Obra:</b> {_html(job.title_name)}\n"
                f"<b>Capitulo:</b> {_html(job.chapter_number)}\n"
                "Status: <b>na fila</b>"
            ),
            parse_mode="HTML",
        )
        status_messages.append(status)

    _active_jobs[job.chapter_id] = {
        "waiters": [{"chat_id": job.chat_id, "caption": job.caption}],
        "status_messages": status_messages,
    }

    queue = bulk_queue if job.is_bulk else single_queue
    try:
        await queue.put(job)
    except BaseException:
        _active_jobs.pop(job.chapter_id, None)
        raise
    return single_queue.qsize() + bulk_queue.qsize()


async def start_pdf_workers(app):
    if app.bot_data.get("pdf_workers_started"):
        return

    app.bot_data["single_pdf_queue"] = asyncio.Queue(maxsize=PDF_QUEUE_LIMIT)
    app.bot_data["bulk_pdf_queue"] = asyncio.Queue(maxsize=PDF_QUEUE_LIMIT)

    for _ in range(PDF_WORKERS_SINGLE):
        _single_workers.append(asyncio.create_task(_worker(app, app.bot_data["single_pdf_queue"])))
    for _ in range(PDF_WORKERS_BULK):
        _bulk_workers.append(asyncio.create_task(_worker(app, app.bot_data["bulk_pdf_queue"])))

    app.bot_data["pdf_workers_started"] = True


async def stop_pdf_workers(app):
    for queue_name, workers in (
        ("single_pdf_queue", _single_workers),
        ("bulk_pdf_queue", _bulk_workers),
    ):
        queue = app.bot_data.get(queue_name)
        if queue is None:
            continue
        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers, return_exceptions=True)
        workers.clear()
