from __future__ import annotations

import html
import json
import time
from pathlib import Path

from config import DATA_DIR, DOCUMENT_ARCHIVE_CHANNEL, PDF_PROTECT_CONTENT

ARCHIVE_INDEX_PATH = Path(DATA_DIR) / "document_file_ids.json"


def document_key(kind: str, chapter_id: str) -> str:
    return f"{str(kind or '').strip().lower()}:{str(chapter_id or '').strip()}"


def _archive_chat_id() -> int | str:
    raw = str(DOCUMENT_ARCHIVE_CHANNEL or "").strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _load_index() -> dict:
    if not ARCHIVE_INDEX_PATH.exists():
        return {}
    try:
        data = json.loads(ARCHIVE_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_index(data: dict) -> None:
    ARCHIVE_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARCHIVE_INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(ARCHIVE_INDEX_PATH)


def get_archived_document(kind: str, chapter_id: str) -> dict:
    return _load_index().get(document_key(kind, chapter_id)) or {}


def forget_archived_document(kind: str, chapter_id: str) -> None:
    data = _load_index()
    data.pop(document_key(kind, chapter_id), None)
    _save_index(data)


def _message_id_from_sent(sent) -> int | None:
    if isinstance(sent, (list, tuple)) and sent:
        sent = sent[0]
    for attr in ("message_id", "id"):
        value = getattr(sent, attr, None)
        if value:
            try:
                return int(value)
            except Exception:
                return None
    return None


async def copy_archived_document(bot, chat_id: int, entry: dict, caption: str) -> bool:
    message_id = entry.get("archive_message_id")
    archive_chat_id = entry.get("archive_chat_id") or DOCUMENT_ARCHIVE_CHANNEL
    if not message_id or not archive_chat_id:
        return False
    try:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=archive_chat_id,
            message_id=int(message_id),
            caption=caption,
            parse_mode="HTML",
            protect_content=PDF_PROTECT_CONTENT,
        )
        return True
    except Exception as error:
        print(f"[DOCUMENT_ARCHIVE] copy_failed entry={entry!r} error={error!r}")
        return False


async def archive_document(
    bot,
    *,
    kind: str,
    chapter_id: str,
    title_name: str,
    chapter_number: str,
    file_path: str,
    file_name: str,
    caption: str,
) -> dict:
    archive_chat_id = _archive_chat_id()
    if not archive_chat_id:
        return {}

    archive_caption = (
        f"{caption}\n\n"
        f"<code>{html.escape(document_key(kind, chapter_id))}</code>"
    )
    with open(file_path, "rb") as file:
        sent = await bot.send_document(
            chat_id=archive_chat_id,
            document=file,
            filename=file_name,
            caption=archive_caption,
            parse_mode="HTML",
            protect_content=False,
        )
    message_id = _message_id_from_sent(sent)
    if not message_id:
        return {}

    entry = {
        "archive_chat_id": str(archive_chat_id),
        "archive_message_id": message_id,
        "kind": kind,
        "chapter_id": chapter_id,
        "chapter_number": chapter_number,
        "title_name": title_name,
        "file_name": file_name,
        "caption": caption,
        "created_at": int(time.time()),
    }
    data = _load_index()
    data[document_key(kind, chapter_id)] = entry
    _save_index(data)
    return entry
