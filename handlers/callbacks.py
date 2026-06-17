import asyncio
import html
import re
import time
from urllib.parse import urlencode

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update, WebAppInfo
from telegram.ext import ContextTypes

from core.background import fire_and_forget, fire_and_forget_sync, run_sync
from config import BOT_BRAND, CHAPTERS_PER_PAGE, DISTRIBUTION_TAG, PDF_BULK_SUBSCRIBE_URL, PREFERRED_CHAPTER_LANG, WEBAPP_BASE_URL
from core.epub_queue import EpubJob, enqueue_epub_job
from core.pdf_queue import PdfJob, enqueue_pdf_job
from handlers.pdf_bulk import (
    can_use_pdf_bulk,
    normalize_pdf_bulk_order,
    request_pdf_bulk_for_title,
    stop_pdf_bulk,
)
from handlers.plan import send_plan_panel
from handlers.search import edit_search_page, render_search_page
from handlers.language import handle_language_callback
from services.catalog_client import (
    flatten_chapters,
    get_cached_chapter_reader_payload,
    get_cached_title_overview,
    get_cached_title_bundle,
    get_cached_title_summary,
    get_chapter_list,
    get_chapter_list_fast,
    get_chapter_reader_payload,
    get_title_bundle,
    get_title_chapters_snapshot,
    prefetch_reader_payloads,
    prefetch_title_bundles,
)
from services.cakto_gateway import get_checkout_options
from services.cakto_api import cakto_api_configured, verify_cakto_payment_for_user
from services.metrics import (
    get_last_read_entry,
    get_read_chapter_ids,
    log_event,
    mark_chapter_read,
)
from services.language_prefs import (
    bundle_language_options,
    get_user_interface_language,
    get_user_language,
    language_badge,
    language_flag,
    normalize_language,
    set_user_language,
)
from services.telegraph_service import get_cached_chapter_page_url, get_or_create_chapter_page

CALLBACK_COOLDOWN = 0.8
TELEGRAPH_INLINE_WAIT = 1.15
CHAPTER_PANEL_FAST_TIMEOUT = 16.0
START_TITLE_RESOLVE_TIMEOUT = 24.0
LANGUAGE_PANEL_TIMEOUT = 6.0
SUPPORT_BOT_URL = "https://t.me/QGSuporteBot"

_USER_CALLBACK_LOCKS: dict[int, asyncio.Lock] = {}
_MESSAGE_EDIT_LOCKS: dict[str, asyncio.Lock] = {}
_MESSAGE_INFLIGHT_ACTIONS: dict[str, str] = {}
_MESSAGE_PANEL_STATE: dict[str, tuple[str, str]] = {}
_LANGUAGE_REFRESH_INFLIGHT: dict[str, float] = {}


def _user_lang(user_id: int | None) -> str:
    return get_user_language(user_id, PREFERRED_CHAPTER_LANG)


def _now() -> float:
    return time.monotonic()


def _callback_last_key(user_id: int) -> str:
    return f"callback_last:{user_id}"


def _callback_data_last_key(user_id: int) -> str:
    return f"callback_data_last:{user_id}"


def _user_lock(user_id: int) -> asyncio.Lock:
    lock = _USER_CALLBACK_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _USER_CALLBACK_LOCKS[user_id] = lock
    return lock


def _message_lock(chat_id: int, message_id: int) -> asyncio.Lock:
    key = f"{chat_id}:{message_id}"
    lock = _MESSAGE_EDIT_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _MESSAGE_EDIT_LOCKS[key] = lock
    return lock


def _action_signature(data: str) -> str:
    parts = (data or "").split("|")
    if len(parts) >= 3:
        return "|".join(parts[:3])
    return data or ""


def _message_action_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def _get_inflight_action(chat_id: int, message_id: int) -> str:
    return _MESSAGE_INFLIGHT_ACTIONS.get(_message_action_key(chat_id, message_id), "")


def _set_inflight_action(chat_id: int, message_id: int, action: str) -> None:
    _MESSAGE_INFLIGHT_ACTIONS[_message_action_key(chat_id, message_id)] = action


def _clear_inflight_action(chat_id: int, message_id: int) -> None:
    _MESSAGE_INFLIGHT_ACTIONS.pop(_message_action_key(chat_id, message_id), None)


def _set_panel_state(chat_id: int, message_id: int, kind: str, ref: str) -> None:
    _MESSAGE_PANEL_STATE[_message_action_key(chat_id, message_id)] = (kind, ref)


def _get_panel_state(chat_id: int, message_id: int) -> tuple[str, str]:
    return _MESSAGE_PANEL_STATE.get(_message_action_key(chat_id, message_id), ("", ""))


def _get_cached_chapter_list_compat(title_id: str, lang: str | None = None) -> dict | None:
    try:
        from services import catalog_client

        getter = getattr(catalog_client, "get_cached_chapter_list", None)
        if not callable(getter):
            return None
        cached = getter(title_id, lang)
        return cached if isinstance(cached, dict) else None
    except Exception:
        return None


def _is_callback_cooldown(context: ContextTypes.DEFAULT_TYPE, user_id: int, data: str) -> bool:
    last_ts = context.user_data.get(_callback_last_key(user_id), 0.0)
    last_data = context.user_data.get(_callback_data_last_key(user_id), "")
    now = _now()

    if data and last_data == data and (now - last_ts) < CALLBACK_COOLDOWN:
        return True

    context.user_data[_callback_last_key(user_id)] = now
    context.user_data[_callback_data_last_key(user_id)] = data
    return False


async def _safe_answer_query(query, text: str | None = None, show_alert: bool = False) -> None:
    try:
        if text is None:
            await query.answer()
        else:
            await query.answer(text, show_alert=show_alert)
    except Exception:
        pass


def _pick_bundle_image(bundle: dict) -> str:
    return (
        bundle.get("cover_url")
        or bundle.get("background_url")
        or ""
    ).strip()


def _summary_latest_chapter(summary: dict) -> dict | None:
    latest = summary.get("latest_chapter")
    if isinstance(latest, dict):
        return latest

    chapter_id = str(summary.get("chapter_id") or "").strip()
    if not chapter_id:
        return None

    return {
        "chapter_id": chapter_id,
        "chapter_number": str(latest or "").strip(),
        "chapter_language": summary.get("language") or PREFERRED_CHAPTER_LANG,
    }


def _latest_chapter_dict(bundle: dict) -> dict:
    latest = (bundle or {}).get("latest_chapter")
    return latest if isinstance(latest, dict) else {}


def _fallback_title_bundle(title_id: str, *, title: str = "Manga", summary: dict | None = None) -> dict:
    title_id = str(title_id or "").strip()
    summary = summary or get_cached_title_summary(title_id) or {}
    total_chapters = 0
    for key in ("total_chapters", "chapters_count", "chapter_count", "anilist_chapters"):
        value = summary.get(key)
        if value in (None, "", []):
            continue
        try:
            total_chapters = max(0, int(value))
            break
        except (TypeError, ValueError):
            continue
    display_title = (
        summary.get("display_title")
        or summary.get("title")
        or title
        or "Manga"
    )
    cover_url = summary.get("cover_url") or ""
    chapter_id = str(summary.get("chapter_id") or "").strip()
    chapter_number = str(summary.get("latest_chapter") or summary.get("chapter_number") or "").strip()
    chapter_language = PREFERRED_CHAPTER_LANG
    chapters = []
    if chapter_id:
        chapter = {
            "chapter_number": chapter_number or "?",
            "chapter_number_float": chapter_number or "0",
            "sort_value": 0,
            "translations": [
                {
                    "id": chapter_id,
                    "url": summary.get("chapter_url") or "",
                    "language": chapter_language,
                    "volume": "",
                    "name": "",
                    "group_name": "",
                    "date": summary.get("updated_at") or "",
                }
            ],
            "preferred_translation": {
                "id": chapter_id,
                "url": summary.get("chapter_url") or "",
                "language": chapter_language,
                "volume": "",
                "name": "",
                "group_name": "",
                "date": summary.get("updated_at") or "",
            },
        }
        chapters.append(chapter)

    return {
        "title_id": title_id,
        "title": display_title,
        "display_title": display_title,
        "cover_url": cover_url,
        "background_url": summary.get("background_url") or cover_url,
        "status": summary.get("status") or summary.get("anilist_status") or "carregando",
        "rating": summary.get("rating") or summary.get("anilist_score") or "",
        "chapters": chapters,
        "languages": summary.get("languages") or [],
        "total_chapters": total_chapters or len(chapters),
        "latest_chapter": _summary_latest_chapter(summary),
        "genres": summary.get("genres") or summary.get("anilist_genres") or [],
        "chapters_partial": True,
    }


async def _load_title_panel_bundle(title_id: str, lang: str | None = None) -> dict:
    resolved_lang = normalize_language(lang) or PREFERRED_CHAPTER_LANG
    cached = get_cached_title_bundle(title_id, resolved_lang) or get_cached_title_overview(title_id)
    if cached is not None and (cached.get("chapters") or not cached.get("chapters_partial")):
        return cached

    summary = get_cached_title_summary(title_id)
    fallback = _fallback_title_bundle(title_id, summary=summary)
    try:
        resolved = await asyncio.wait_for(get_title_chapters_snapshot(title_id, resolved_lang), timeout=START_TITLE_RESOLVE_TIMEOUT)
    except Exception as error:
        print("[TITLE_PANEL][SNAPSHOT_FALLBACK]", title_id, repr(error))
        return cached or fallback
    if isinstance(resolved, dict) and (resolved.get("chapters") or resolved.get("cover_url") or resolved.get("background_url")):
        return {
            **fallback,
            **(cached or {}),
            **resolved,
            "title_id": str((resolved.get("title_id") or title_id)).strip(),
        }
    return cached or fallback


def _pick_chapter_image(chapter: dict) -> str:
    return (
        chapter.get("cover_url")
        or chapter.get("background_url")
        or ""
    ).strip()


def _truncate(text: str, limit: int = 320) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _display_score(bundle: dict) -> str:
    score = bundle.get("anilist_score") or bundle.get("rating") or ""
    if score in ("", None, 0):
        return ""
    return str(score)


def _ui_lang(user_id: int | None) -> str:
    locale = str(get_user_interface_language(user_id) or "pt-BR").lower()
    if locale.startswith("es"):
        return "es"
    if locale.startswith("en"):
        return "en"
    return "pt"


_STATUS_LABELS = {
    "ongoing": {"pt": "Em andamento", "en": "Ongoing", "es": "En emisión"},
    "completed": {"pt": "Concluído", "en": "Completed", "es": "Finalizado"},
    "complete": {"pt": "Concluído", "en": "Completed", "es": "Finalizado"},
    "hiatus": {"pt": "Em hiato", "en": "On hiatus", "es": "En pausa"},
    "cancelled": {"pt": "Cancelado", "en": "Cancelled", "es": "Cancelado"},
    "publishing": {"pt": "Em andamento", "en": "Publishing", "es": "En publicación"},
    "finished": {"pt": "Concluído", "en": "Finished", "es": "Finalizado"},
}


_TITLE_BUTTONS = {
    "latest": {"pt": "🆕 Último capítulo", "en": "🆕 Latest chapter", "es": "🆕 Último capítulo"},
    "list": {"pt": "📜 Lista de capítulos", "en": "📜 Chapter list", "es": "📜 Lista de capítulos"},
    "offline": {"pt": "📥 Ler off-line", "en": "📥 Read offline", "es": "📥 Leer sin conexión"},
}


_ALLOWED_TAG_TRANSLATIONS = {'romance': ('Romance', 'Romance', 'Romance'), 'comedy': ('Comédia', 'Comedy', 'Comedia'), 'drama': ('Drama', 'Drama', 'Drama'), 'fantasy': ('Fantasia', 'Fantasy', 'Fantasía'), 'slice of life': ('Cotidiano', 'Slice of Life', 'Recuentos de Vida'), 'action': ('Ação', 'Action', 'Acción'), 'boys love': ('Boys Love', 'Boys Love', 'Boys Love'), 'adventure': ('Aventura', 'Adventure', 'Aventura'), 'psychological': ('Psicológico', 'Psychological', 'Psicológico'), 'mystery': ('Mistério', 'Mystery', 'Misterio'), 'historical': ('Histórico', 'Historical', 'Histórico'), 'girls love': ('Girls Love', 'Girls Love', 'Girls Love'), 'tragedy': ('Tragédia', 'Tragedy', 'Tragedia'), 'sci fi': ('Ficção Científica', 'Sci Fi', 'Ciencia Ficción'), 'horror': ('Terror', 'Horror', 'Terror'), 'isekai': ('Isekai', 'Isekai', 'Isekai'), 'sports': ('Esportes', 'Sports', 'Deportes'), 'thriller': ('Suspense', 'Thriller', 'Suspenso'), 'adult': ('Adulto', 'Adult', 'Adulto'), 'crime': ('Crime', 'Crime', 'Crimen'), 'yaoi': ('Yaoi', 'Yaoi', 'Yaoi'), 'mecha': ('Mecha', 'Mecha', 'Mecha'), 'philosophical': ('Filosófico', 'Philosophical', 'Filosófico'), 'smut': ('Smut', 'Smut', 'Smut'), 'mature': ('Maduro', 'Mature', 'Maduro'), 'wuxia': ('Wuxia', 'Wuxia', 'Wuxia'), 'medical': ('Médico', 'Medical', 'Médico'), 'superhero': ('Super Herói', 'Superhero', 'Superhéroe'), 'magical girls': ('Garotas Mágicas', 'Magical Girls', 'Chicas Mágicas'), 'ecchi': ('Ecchi', 'Ecchi', 'Ecchi'), 'yuri': ('Yuri', 'Yuri', 'Yuri'), 'shounen ai': ('Shounen Ai', 'Shounen Ai', 'Shounen Ai'), 'user created': ('Criado por Usuário', 'User Created', 'Creado por Usuario'), 'revenge': ('Vingança', 'Revenge', 'Venganza'), 'shoujo g': ('Shoujo', 'Shoujo', 'Shoujo'), 'josei w': ('Josei', 'Josei', 'Josei'), 'hentai': ('Hentai', 'Hentai', 'Hentai'), 'school life': ('Vida Escolar', 'School Life', 'Vida Escolar'), 'supernatural': ('Sobrenatural', 'Supernatural', 'Sobrenatural'), 'comics': ('Quadrinhos', 'Comics', 'Cómics'), 'magic': ('Magia', 'Magic', 'Magia'), 'monsters': ('Monstros', 'Monsters', 'Monstruos'), 'martial arts': ('Artes Marciais', 'Martial Arts', 'Artes Marciales'), 'animals': ('Animais', 'Animals', 'Animales'), 'reincarnation': ('Reencarnação', 'Reincarnation', 'Reencarnación'), 'demons': ('Demônios', 'Demons', 'Demonios'), 'office workers': ('Escritório', 'Office Workers', 'Oficinistas'), 'harem': ('Harém', 'Harem', 'Harén'), 'survival': ('Sobrevivência', 'Survival', 'Supervivencia'), 'military': ('Militar', 'Military', 'Militar'), 'crossdressing': ('Crossdressing', 'Crossdressing', 'Travestismo'), 'video games': ('Jogos', 'Video Games', 'Videojuegos'), 'delinquents': ('Delinquentes', 'Delinquents', 'Delincuentes'), 'ghosts': ('Fantasmas', 'Ghosts', 'Fantasmas'), 'time travel': ('Viagem no Tempo', 'Time Travel', 'Viaje en el Tiempo'), 'cooking': ('Culinária', 'Cooking', 'Cocina'), 'monster girls': ('Garotas Monstro', 'Monster Girls', 'Chicas Monstruo'), 'police': ('Polícia', 'Police', 'Policía'), 'aliens': ('Alienígenas', 'Aliens', 'Alienígenas'), 'music': ('Música', 'Music', 'Música'), 'genderswap': ('Troca de Gênero', 'Genderswap', 'Cambio de Género'), 'vampires': ('Vampiros', 'Vampires', 'Vampiros'), 'mafia': ('Máfia', 'Mafia', 'Mafia'), 'samurai': ('Samurai', 'Samurai', 'Samurái'), 'post apocalyptic': ('Pós Apocalíptico', 'Post Apocalyptic', 'Postapocalíptico'), 'shota': ('Shota', 'Shota', 'Shota'), 'villainess': ('Vilã', 'Villainess', 'Villana'), 'incest': ('Incesto', 'Incest', 'Incesto'), 'reverse harem': ('Harém Reverso', 'Reverse Harem', 'Harén Inverso'), 'gyaru': ('Gyaru', 'Gyaru', 'Gyaru'), 'ninja': ('Ninja', 'Ninja', 'Ninja'), 'zombies': ('Zumbis', 'Zombies', 'Zombis'), 'loli': ('Loli', 'Loli', 'Loli'), 'traditional games': ('Jogos Tradicionais', 'Traditional Games', 'Juegos Tradicionales'), 'virtual reality': ('Realidade Virtual', 'Virtual Reality', 'Realidad Virtual'), 'manhwa 18': ('Manhwa 18+', 'Manhwa 18+', 'Manhwa 18+')}


def _norm_tag_label(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _status_label(value: object, user_id: int | None) -> str:
    lang = _ui_lang(user_id)
    key = _norm_tag_label(value)
    labels = _STATUS_LABELS.get(key)
    if labels:
        return labels.get(lang) or labels["pt"]
    return str(value or ({"pt": "Não informado", "en": "Unknown", "es": "No informado"}[lang])).strip()


def _title_button_label(key: str, user_id: int | None) -> str:
    lang = _ui_lang(user_id)
    return _TITLE_BUTTONS[key].get(lang) or _TITLE_BUTTONS[key]["pt"]


def _hashtag(label: str) -> str:
    clean = re.sub(r"[^\wÀ-ÿ]+", "", label, flags=re.UNICODE)
    return f"#{clean}" if clean else ""


def _title_genre_hashtags(bundle: dict, user_id: int | None, limit: int = 4) -> str:
    lang = _ui_lang(user_id)
    lang_index = {"pt": 0, "en": 1, "es": 2}[lang]
    raw_tags = []
    for key in ("genres", "anilist_genres", "tags"):
        value = bundle.get(key) or []
        if isinstance(value, str):
            raw_tags.extend(part.strip() for part in re.split(r"[,|/•]+|\s+(?=#)", value))
        else:
            raw_tags.extend(value)

    seen: set[str] = set()
    output: list[str] = []
    for item in raw_tags:
        if isinstance(item, dict):
            item = item.get("name") or item.get("label") or item.get("title") or ""
        norm = _norm_tag_label(item)
        translated = _ALLOWED_TAG_TRANSLATIONS.get(norm)
        if not translated or norm in seen:
            continue
        seen.add(norm)
        tag = _hashtag(translated[lang_index])
        if tag:
            output.append(tag)
        if len(output) >= limit:
            break

    if output:
        return ", ".join(output)
    fallback = {"pt": "Não informado", "en": "Not listed", "es": "No informado"}
    return fallback[lang]


def _miniapp_url(
    *,
    title_id: str = "",
    chapter_id: str = "",
    page: str = "",
    route: str = "",
    lang: str = "",
    user_id: int | str | None = None,
    source: str = "bot",
) -> str:
    params: dict[str, str] = {"source": source}

    if user_id:
        uid = str(user_id).strip()
        if uid.isdigit():
            params["user_id"] = uid

    if title_id:
        tid = str(title_id).strip()
        params["title_id"] = tid
        params["manga_id"] = tid
        params["id"] = tid
        params["title"] = tid

    if chapter_id:
        cid = str(chapter_id).strip()
        params["chapter_id"] = cid
        params["cap"] = cid
        params["read"] = cid
        params["chapter"] = cid

    if page:
        pg = str(page).strip()
        params["page"] = pg
        params["view"] = pg

    resolved_lang = normalize_language(lang)
    if resolved_lang:
        params["lang"] = resolved_lang

    resolved_route = route.strip() if route else ""
    if not resolved_route:
        if chapter_id:
            resolved_route = "reader"
        elif title_id and page == "chapters":
            resolved_route = "chapters"
        elif title_id:
            resolved_route = "detail"
        else:
            resolved_route = "home"

    params["route"] = resolved_route

    base = WEBAPP_BASE_URL.rstrip("/")
    query = urlencode(params)
    return f"{base}/miniapp/index.html?{query}" if query else f"{base}/miniapp/index.html"


def _title_text(bundle: dict, last_read: dict | None = None, user_id: int | None = None) -> str:
    title = html.escape(bundle.get("title") or "Manga")
    is_partial = bool(bundle.get("chapters_partial"))
    raw_status = bundle.get("status") or bundle.get("anilist_status") or ""
    raw_chapters = bundle.get("total_chapters") or bundle.get("anilist_chapters") or ""
    status = html.escape(_status_label(raw_status or ("carregando" if is_partial else ""), user_id))
    chapters = html.escape(str(raw_chapters or ("carregando" if is_partial else "?")))
    score = _display_score(bundle) or "0"
    score_text = html.escape(score if "/" in str(score) else f"{score}/10")
    genres_text = html.escape(_title_genre_hashtags(bundle, user_id))

    lang = _ui_lang(user_id)
    footer = {
        "pt": "✨ <i>Escolha abaixo como quer continuar.</i>",
        "en": "✨ <i>Choose below how you want to continue.</i>",
        "es": "✨ <i>Elige abajo cómo quieres continuar.</i>",
    }[lang]
    if is_partial:
        footer = {
            "pt": "⏳ <i>Abri a obra. Vou atualizar este card em instantes.</i>",
            "en": "⏳ <i>I opened the title. I will update this card in a moment.</i>",
            "es": "⏳ <i>Abrí la obra. Actualizaré esta tarjeta en un momento.</i>",
        }[lang]

    return (
        f"📚 <b>{title}</b>\n\n"
        f"<blockquote><b>Status:</b> <i>{status}</i>\n"
        f"<b>Capítulos:</b> <i>{chapters}</i>\n"
        f"<b>Nota:</b> <i>{score_text}</i>\n"
        f"<b>Gêneros:</b> <i>{genres_text}</i></blockquote>\n\n"
        f"{footer}"
    )


def _title_keyboard(bundle: dict, last_read: dict | None = None, user_id: int | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    title_id = str(bundle.get("title_id") or "").strip()
    latest_chapter = _latest_chapter_dict(bundle)
    lang = _user_lang(user_id)

    if latest_chapter.get("chapter_id"):
        rows.append([
            InlineKeyboardButton(
                _title_button_label("latest", user_id),
                web_app=WebAppInfo(
                    url=_miniapp_url(
                        title_id=title_id,
                        chapter_id=latest_chapter["chapter_id"],
                        route="reader",
                        lang=lang,
                        user_id=user_id,
                    )
                ),
            )
        ])

    if title_id:
        rows.append([
            InlineKeyboardButton(
                _title_button_label("list", user_id),
                web_app=WebAppInfo(
                    url=_miniapp_url(
                        title_id=title_id,
                        page="chapters",
                        route="chapters",
                        lang=lang,
                        user_id=user_id,
                    )
                ),
            )
        ])
        rows.append([
            InlineKeyboardButton(_title_button_label("offline", user_id), callback_data=f"mb|offline|{title_id}")
        ])

    return InlineKeyboardMarkup(rows)



def _language_text(bundle: dict, user_id: int | None) -> str:
    title = html.escape(bundle.get("title") or "Manga")
    current_lang = _user_lang(user_id)
    current = html.escape(f"{current_lang.upper()} {language_flag(current_lang)}")
    options = bundle_language_options(bundle, include_default=False)
    total = len(options)
    footer = "Escolha abaixo o idioma dos capítulos."
    if total == 0 or bundle.get("languages_loading"):
        footer = "Ainda estou buscando os idiomas reais desta obra. Toque em recarregar em instantes."
    return (
        f"🌎 <b>Idioma</b>\n\n"
        f"» <b>Obra:</b> <i>{title}</i>\n"
        f"» <b>Atual:</b> <i>{current}</i>\n"
        f"» <b>Disponíveis:</b> <i>{total if total else 'carregando'}</i>\n\n"
        f"{footer}"
    )


def _language_keyboard(bundle: dict, user_id: int | None) -> InlineKeyboardMarkup:
    title_id = str(bundle.get("title_id") or "").strip()
    current = _user_lang(user_id)
    rows: list[list[InlineKeyboardButton]] = []
    line: list[InlineKeyboardButton] = []

    options = bundle_language_options(bundle, include_default=False)
    for option in options:
        code = option["code"]
        prefix = "🔘 " if code == current else ""
        label = f"{prefix}{option.get('short') or code.upper()} {option.get('flag') or language_flag(code)}"
        line.append(InlineKeyboardButton(label, callback_data=f"mb|setlang|{title_id}|{code}"))
        if len(line) == 3:
            rows.append(line)
            line = []
    if line:
        rows.append(line)

    if not options or bundle.get("languages_loading"):
        rows.append([InlineKeyboardButton("🔄 Recarregar idiomas", callback_data=f"mb|lang|{title_id}")])

    rows.append([InlineKeyboardButton("🔙 Voltar para a obra", callback_data=f"mb|title|{title_id}")])
    return InlineKeyboardMarkup(rows)


def _offline_text(bundle: dict) -> str:
    title = html.escape(bundle.get("title") or "Manga")
    is_partial = bool(bundle.get("chapters_partial"))
    chapters = html.escape(str(bundle.get("total_chapters") or ("carregando" if is_partial else "?")))
    footer = "✨ <i>Escolha como quer receber os PDFs.</i>"
    if is_partial:
        footer = "⏳ <i>A lista completa ainda está carregando. Você já pode escolher a ordem.</i>"

    return (
        f"📥 <b>Ler offline</b>\n\n"
        f"» <b>Obra:</b> <i>{title}</i>\n"
        f"» <b>Capítulos:</b> <i>{chapters}</i>\n\n"
        f"{footer}"
    )


def _offline_keyboard(bundle: dict) -> InlineKeyboardMarkup:
    title_id = str(bundle.get("title_id") or "").strip()
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📥 Primeiro ao último", callback_data=f"mb|pdfall|{title_id}|asc")],
            [InlineKeyboardButton("📥 Último ao primeiro", callback_data=f"mb|pdfall|{title_id}|desc")],
            [InlineKeyboardButton("🔙 Voltar para a obra", callback_data=f"mb|title|{title_id}")],
        ]
    )


def _chapter_download_label(item: dict) -> str:
    number = str(item.get("chapter_number") or "?").strip()
    lowered = number.lower()
    for prefix in ("ch.", "ch", "capitulo", "capítulo", "cap."):
        if lowered.startswith(prefix):
            number = number[len(prefix):].strip(" .:-")
            break
    return number or "?"


def _offline_chapters_text(bundle: dict, page: int, total_items: int) -> str:
    total_pages = max(1, ((total_items - 1) // CHAPTERS_PER_PAGE) + 1)
    title = html.escape(bundle.get("title") or "Manga")

    if bundle.get("chapters_partial") and total_items <= 0:
        return (
            f"📥 <b>Ler offline</b>\n\n"
            f"» <b>Obra:</b> <i>{title}</i>\n"
            "» <b>Status:</b> <i>carregando capítulos</i>\n\n"
            "⏳ <i>A lista ainda está carregando. Tente novamente em alguns segundos.</i>"
        )

    return (
        f"📥 <b>Ler offline</b>\n\n"
        f"» <b>Obra:</b> <i>{title}</i>\n"
        f"» <b>Página:</b> <i>{page}/{total_pages}</i>\n"
        f"» <b>Capítulos:</b> <i>{total_items}</i>\n\n"
        "Escolha um capítulo para baixar em PDF ou EPUB."
    )


def _offline_chapters_keyboard(bundle: dict, chapters: list[dict], page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    title_id = str(bundle.get("title_id") or "").strip()
    total_items = len(chapters)
    total_pages = max(1, ((total_items - 1) // CHAPTERS_PER_PAGE) + 1)
    page = max(1, min(page, total_pages))
    start = (page - 1) * CHAPTERS_PER_PAGE
    end = min(start + CHAPTERS_PER_PAGE, total_items)

    line: list[InlineKeyboardButton] = []
    for item in chapters[start:end]:
        chapter_id = str(item.get("chapter_id") or "").strip()
        if not chapter_id:
            continue
        line.append(
            InlineKeyboardButton(
                _chapter_download_label(item),
                callback_data=f"mb|offread|{chapter_id}|{page}",
            )
        )
        if len(line) == 3:
            rows.append(line)
            line = []
    if line:
        rows.append(line)

    if not chapters and bundle.get("chapters_partial"):
        rows.append([InlineKeyboardButton("⏳ Tentar novamente", callback_data=f"mb|offchap|{title_id}|1")])

    rows.append(
        [
            InlineKeyboardButton("⏪", callback_data=f"mb|offchap|{title_id}|1" if page > 1 else "mb|noop"),
            InlineKeyboardButton("⬅️", callback_data=f"mb|offchap|{title_id}|{page - 1}" if page > 1 else "mb|noop"),
            InlineKeyboardButton(f"{page}/{total_pages}", callback_data="mb|noop"),
            InlineKeyboardButton("➡️", callback_data=f"mb|offchap|{title_id}|{page + 1}" if page < total_pages else "mb|noop"),
            InlineKeyboardButton("⏩", callback_data=f"mb|offchap|{title_id}|{total_pages}" if page < total_pages else "mb|noop"),
        ]
    )

    if title_id:
        rows.append([InlineKeyboardButton("📦 Baixar todos", callback_data=f"mb|offbulk|{title_id}")])
        rows.append([InlineKeyboardButton("🔙 Voltar para a obra", callback_data=f"mb|title|{title_id}")])

    return InlineKeyboardMarkup(rows)


def _chapter_page_for(title_id: str, chapter_id: str, fallback_page: int = 1, lang: str | None = None) -> int:
    resolved_lang = normalize_language(lang) or PREFERRED_CHAPTER_LANG
    bundle = get_cached_title_bundle(title_id, resolved_lang)
    chapters = flatten_chapters({"chapters": (bundle or {}).get("chapters") or []}, resolved_lang)
    for index, item in enumerate(chapters):
        if str(item.get("chapter_id") or "") == str(chapter_id or ""):
            return (index // CHAPTERS_PER_PAGE) + 1
    return max(1, int(fallback_page or 1))


def _offline_chapter_text(chapter: dict) -> str:
    title = html.escape(chapter.get("title") or "Manga")
    chapter_number = html.escape(chapter.get("chapter_number") or "?")
    image_count = html.escape(str(chapter.get("image_count") or len(chapter.get("images") or []) or 0))

    return (
        f"📥 <b>Ler offline</b>\n\n"
        f"» <b>Obra:</b> <i>{title}</i>\n"
        f"» <b>Capítulo:</b> <i>{chapter_number}</i>\n"
        f"» <b>Páginas:</b> <i>{image_count}</i>\n\n"
        "Escolha o formato para baixar este capítulo."
    )


def _offline_chapter_keyboard(chapter: dict, page: int = 1) -> InlineKeyboardMarkup:
    title_id = str(chapter.get("title_id") or "").strip()
    chapter_id = str(chapter.get("chapter_id") or "").strip()
    lang = normalize_language(chapter.get("chapter_language")) or PREFERRED_CHAPTER_LANG
    page = _chapter_page_for(title_id, chapter_id, page, lang)

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("📄 Baixar PDF", callback_data=f"mb|offpdf|{chapter_id}|{page}"),
            InlineKeyboardButton("📚 Baixar EPUB", callback_data=f"mb|offepub|{chapter_id}|{page}"),
        ]
    ]

    nav: list[InlineKeyboardButton] = []
    previous_chapter = chapter.get("previous_chapter") or {}
    next_chapter = chapter.get("next_chapter") or {}
    if previous_chapter.get("chapter_id"):
        prev_id = previous_chapter["chapter_id"]
        nav.append(
            InlineKeyboardButton(
                "⬅️ Anterior",
                callback_data=f"mb|offread|{prev_id}|{_chapter_page_for(title_id, prev_id, page, lang)}",
            )
        )
    if next_chapter.get("chapter_id"):
        next_id = next_chapter["chapter_id"]
        nav.append(
            InlineKeyboardButton(
                "Próximo ➡️",
                callback_data=f"mb|offread|{next_id}|{_chapter_page_for(title_id, next_id, page, lang)}",
            )
        )
    if nav:
        rows.append(nav)

    if title_id:
        rows.append([InlineKeyboardButton("📚 Ver capítulos", callback_data=f"mb|offchap|{title_id}|{page}")])

    return InlineKeyboardMarkup(rows)


def _normalize_url(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://", "tg://")):
        return url
    return f"https://{url}"


def _offline_locked_text(bundle: dict) -> str:
    title = html.escape(bundle.get("title") or "Manga")
    brand = html.escape(BOT_BRAND or "Mangas Baltigo")
    return (
        f"🔒 <b>Conteúdo exclusivo para assinantes do {brand}</b>\n\n"
        f"» <b>Obra:</b> <i>{title}</i>\n\n"
        "A leitura offline com todos os capítulos em PDF está bloqueada aqui no bot.\n\n"
        "Escolha um plano abaixo. Assim que a Cakto aprovar o pagamento, "
        "o bot libera seu ID automaticamente."
    )


def _offline_locked_keyboard(bundle: dict, user_id: int | None) -> InlineKeyboardMarkup | None:
    options = get_checkout_options(user_id)
    title_id = str(bundle.get("title_id") or "").strip()
    if options:
        rows = [[InlineKeyboardButton(option["label"], url=option["url"])] for option in options]
        if title_id:
            rows.append([InlineKeyboardButton("🔄 Já paguei / verificar", callback_data=f"mb|paycheck|{title_id}")])
        rows.append([InlineKeyboardButton("🛟 Suporte", url=SUPPORT_BOT_URL)])
        return InlineKeyboardMarkup(rows)

    subscribe_url = _normalize_url(PDF_BULK_SUBSCRIBE_URL)
    if not subscribe_url:
        if title_id:
            return InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Já paguei / verificar", callback_data=f"mb|paycheck|{title_id}")],
                    [InlineKeyboardButton("🛟 Suporte", url=SUPPORT_BOT_URL)],
                ]
            )
        return None
    brand = BOT_BRAND or "Mangas Baltigo"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"✨ Assinar {brand}", url=subscribe_url)],
            [InlineKeyboardButton("🛟 Suporte", url=SUPPORT_BOT_URL)],
        ]
    )


async def _send_offline_locked(target, bundle: dict, user_id: int | None) -> None:
    text = _offline_locked_text(bundle)
    keyboard = _offline_locked_keyboard(bundle, user_id)

    message = getattr(target, "message", None)
    if message:
        try:
            await message.reply_text(
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass

    try:
        await target.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        pass


def _chapter_list_text(bundle: dict, page: int, total_items: int, lang: str = "") -> str:
    total_pages = max(1, ((total_items - 1) // CHAPTERS_PER_PAGE) + 1)
    if bundle.get("chapters_partial") and total_items <= 0:
        return (
            f"📖 <b>{html.escape(bundle.get('title') or 'Manga')}</b>\n\n"
            "» <b>Status:</b> <i>carregando capítulos</i>\n\n"
            "A fonte demorou para responder. Tente novamente em alguns segundos."
        )

    return (
        f"📖 <b>{html.escape(bundle.get('title') or 'Manga')}</b>\n\n"
        f"» <b>Página:</b> <i>{page}/{total_pages}</i>\n"
        f"» <b>Idioma:</b> <i>{html.escape(language_badge(lang or PREFERRED_CHAPTER_LANG))}</i>\n"
        f"» <b>Capítulos disponíveis:</b> <i>{total_items}</i>\n\n"
        "Toque em um capítulo abaixo.\n"
        "✅ = capítulo já lido"
    )


def _chapter_button_label(item: dict, read_ids: set[str]) -> str:
    number = str(item.get("chapter_number") or "?").strip()
    return f"✅ {number}" if item.get("chapter_id") in read_ids else number


def _chapter_list_keyboard(
    bundle: dict,
    chapters: list[dict],
    page: int,
    read_ids: set[str],
    lang: str = "",
    user_id: int | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    total_items = len(chapters)
    total_pages = max(1, ((total_items - 1) // CHAPTERS_PER_PAGE) + 1)
    start = (page - 1) * CHAPTERS_PER_PAGE
    end = min(start + CHAPTERS_PER_PAGE, total_items)
    page_items = chapters[start:end]

    line: list[InlineKeyboardButton] = []
    for item in page_items:
        line.append(
            InlineKeyboardButton(
                _chapter_button_label(item, read_ids),
                web_app=WebAppInfo(
                    url=_miniapp_url(
                        title_id=bundle["title_id"],
                        chapter_id=item["chapter_id"],
                        route="reader",
                        lang=lang,
                        user_id=user_id,
                    )
                ),
            )
        )
        if len(line) == 3:
            rows.append(line)
            line = []
    if line:
        rows.append(line)

    if not chapters and bundle.get("chapters_partial"):
        rows.append([InlineKeyboardButton("⏳ Tentar novamente", callback_data=f"mb|chap|{bundle['title_id']}|1")])

    rows.append([InlineKeyboardButton(f"{page}/{total_pages}", callback_data="mb|noop")])

    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("⏪", callback_data=f"mb|chap|{bundle['title_id']}|1"))
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"mb|chap|{bundle['title_id']}|{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"mb|chap|{bundle['title_id']}|{page + 1}"))
        nav.append(InlineKeyboardButton("⏩", callback_data=f"mb|chap|{bundle['title_id']}|{total_pages}"))
    if nav:
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                "🔙 Voltar para a obra",
                web_app=WebAppInfo(
                    url=_miniapp_url(title_id=bundle["title_id"], route="detail", user_id=user_id)
                ),
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _chapter_text(chapter: dict) -> str:
    title = html.escape(chapter.get("title") or "Manga")
    chapter_number = html.escape(chapter.get("chapter_number") or "?")
    lang = html.escape((chapter.get("chapter_language") or PREFERRED_CHAPTER_LANG).upper())
    image_count = html.escape(str(chapter.get("image_count") or 0))

    return (
        f"📖 <b>{title}</b>\n\n"
        f"» <b>Capítulo:</b> <i>{chapter_number}</i>\n"
        f"» <b>Idioma:</b> <i>{lang}</i>\n"
        f"» <b>Páginas:</b> <i>{image_count}</i>\n\n"
        "✨ <i>Escolha abaixo se quer leitura rápida ou PDF.</i>"
    )


def _chapter_keyboard(
    chapter: dict,
    telegraph_url: str = "",
    *,
    telegraph_pending: bool = False,
    user_id: int | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    rows.append(
        [
            InlineKeyboardButton(
                "📰 Abrir leitura rápida" if telegraph_url else ("⏳ Preparando leitura rápida" if telegraph_pending else "📰 Leitura rápida"),
                web_app=WebAppInfo(
                    url=_miniapp_url(
                        title_id=chapter["title_id"],
                        chapter_id=chapter["chapter_id"],
                        route="reader",
                        user_id=user_id,
                    )
                ),
            )
        ]
    )
    rows.append([InlineKeyboardButton("📄 Baixar PDF", callback_data=f"mb|pdf|{chapter['chapter_id']}")])

    nav: list[InlineKeyboardButton] = []
    if chapter.get("previous_chapter"):
        nav.append(
            InlineKeyboardButton(
                "⬅️ Anterior",
                web_app=WebAppInfo(
                    url=_miniapp_url(
                        title_id=chapter["title_id"],
                        chapter_id=chapter["previous_chapter"]["chapter_id"],
                        route="reader",
                        user_id=user_id,
                    )
                ),
            )
        )
    if chapter.get("next_chapter"):
        nav.append(
            InlineKeyboardButton(
                "Próximo ➡️",
                web_app=WebAppInfo(
                    url=_miniapp_url(
                        title_id=chapter["title_id"],
                        chapter_id=chapter["next_chapter"]["chapter_id"],
                        route="reader",
                        user_id=user_id,
                    )
                ),
            )
        )
    if nav:
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                "📚 Ver capítulos",
                web_app=WebAppInfo(
                    url=_miniapp_url(title_id=chapter["title_id"], page="chapters", route="chapters", user_id=user_id)
                ),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "🔙 Voltar para a obra",
                web_app=WebAppInfo(
                    url=_miniapp_url(title_id=chapter["title_id"], route="detail", user_id=user_id)
                ),
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _loading_keyboard(label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data="mb|noop")]])


def _payment_check_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⏳ Verificando pagamento", callback_data="mb|noop")],
            [InlineKeyboardButton("🛟 Suporte", url=SUPPORT_BOT_URL)],
        ]
    )


async def _show_loading_markup(query, label: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await query.edit_message_reply_markup(reply_markup=reply_markup or _loading_keyboard(label))
    except Exception:
        pass


async def _restore_reply_markup(query, reply_markup) -> None:
    if reply_markup is None:
        return
    try:
        await query.edit_message_reply_markup(reply_markup=reply_markup)
    except Exception:
        pass


async def _render_panel(target, text: str, keyboard: InlineKeyboardMarkup, photo: str = "", *, edit: bool):
    if edit:
        if photo:
            media = InputMediaPhoto(media=photo, caption=text, parse_mode="HTML")
            try:
                await target.edit_message_media(media=media, reply_markup=keyboard)
                return target.message
            except Exception:
                pass
        try:
            await target.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=keyboard)
            return target.message
        except Exception:
            pass
        try:
            await target.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return target.message
        except Exception:
            pass
        if photo:
            try:
                return await target.message.reply_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=keyboard)
            except Exception:
                pass
        return await target.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)

    if photo:
        try:
            return await target.reply_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            pass
    return await target.reply_text(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)


async def _render_panel_to_message(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    message_id: int,
    text: str,
    keyboard: InlineKeyboardMarkup,
    photo: str = "",
) -> None:
    bot = context.bot

    if photo:
        media = InputMediaPhoto(media=photo, caption=text, parse_mode="HTML")
        try:
            await bot.edit_message_media(chat_id=chat_id, message_id=message_id, media=media, reply_markup=keyboard)
            return
        except Exception:
            pass

    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return
    except Exception:
        pass

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        pass


def _chapter_telegraph_title(chapter: dict) -> str:
    return f"{chapter.get('title') or 'Manga'} - Capítulo {chapter.get('chapter_number') or '?'}"


async def _auto_finalize_telegraph_panel(
    context: ContextTypes.DEFAULT_TYPE,
    panel_message,
    chapter: dict,
    telegraph_task: asyncio.Task,
) -> None:
    if not panel_message:
        return

    try:
        url = await telegraph_task
    except Exception:
        return

    chat_id = getattr(getattr(panel_message, "chat", None), "id", None)
    message_id = getattr(panel_message, "message_id", None)
    if chat_id is None or message_id is None:
        return

    async with _message_lock(chat_id, message_id):
        kind, ref = _get_panel_state(chat_id, message_id)
        if kind != "chapter" or ref != (chapter.get("chapter_id") or ""):
            return

        await _render_panel_to_message(
            context,
            chat_id=chat_id,
            message_id=message_id,
            text=_chapter_text(chapter),
            keyboard=_chapter_keyboard(chapter, telegraph_url=url, user_id=user_id),
            photo=_pick_chapter_image(chapter),
        )


async def _auto_finalize_title_panel(
    context: ContextTypes.DEFAULT_TYPE,
    panel_message,
    title_id: str,
    user_id: int | None,
    title_task: asyncio.Task,
) -> None:
    if not panel_message:
        return

    try:
        bundle = await title_task
    except Exception as error:
        print("[TITLE_PANEL][REFRESH_FAIL]", title_id, repr(error))
        return

    if not isinstance(bundle, dict):
        return

    chat_id = getattr(getattr(panel_message, "chat", None), "id", None)
    message_id = getattr(panel_message, "message_id", None)
    if chat_id is None or message_id is None:
        return

    async with _message_lock(chat_id, message_id):
        kind, ref = _get_panel_state(chat_id, message_id)
        bundle_title_id = str(bundle.get("title_id") or title_id).strip()
        if kind != "title" or ref != bundle_title_id:
            return

        last_read = await run_sync(get_last_read_entry, user_id, bundle_title_id) if user_id else None
        lang = _user_lang(user_id)
        chapters = flatten_chapters({"chapters": bundle.get("chapters") or []}, lang)
        latest = _latest_chapter_dict(bundle)
        chapter_ids = [latest.get("chapter_id") or ""]
        chapter_ids.extend(item.get("chapter_id") or "" for item in chapters[:3])
        prefetch_reader_payloads(chapter_ids, limit=4)

        await _render_panel_to_message(
            context,
            chat_id=chat_id,
            message_id=message_id,
            text=_title_text(bundle, last_read, user_id),
            keyboard=_title_keyboard(bundle, last_read, user_id),
            photo=_pick_bundle_image(bundle),
        )


async def _auto_finalize_offline_panel(
    context: ContextTypes.DEFAULT_TYPE,
    panel_message,
    title_id: str,
    user_id: int | None,
    title_task: asyncio.Task,
) -> None:
    if not panel_message:
        return

    try:
        bundle = await title_task
    except Exception as error:
        print("[OFFLINE_PANEL][REFRESH_FAIL]", title_id, repr(error))
        return

    if not isinstance(bundle, dict):
        return

    chat_id = getattr(getattr(panel_message, "chat", None), "id", None)
    message_id = getattr(panel_message, "message_id", None)
    if chat_id is None or message_id is None:
        return

    async with _message_lock(chat_id, message_id):
        kind, ref = _get_panel_state(chat_id, message_id)
        bundle_title_id = str(bundle.get("title_id") or title_id).strip()
        if kind != "offline" or ref != bundle_title_id:
            return

        lang = _user_lang(user_id)
        chapters = flatten_chapters({"chapters": bundle.get("chapters") or []}, lang)
        await _render_panel_to_message(
            context,
            chat_id=chat_id,
            message_id=message_id,
            text=_offline_chapters_text(bundle, 1, len(chapters)),
            keyboard=_offline_chapters_keyboard(bundle, chapters, 1),
            photo=_pick_bundle_image(bundle),
        )


async def _auto_finalize_language_panel(
    context: ContextTypes.DEFAULT_TYPE,
    panel_message,
    title_id: str,
    user_id: int | None,
) -> None:
    if not panel_message:
        return

    chat_id = getattr(getattr(panel_message, "chat", None), "id", None)
    message_id = getattr(panel_message, "message_id", None)
    if chat_id is None or message_id is None:
        return

    refresh_key = f"{chat_id}:{message_id}:{title_id}"
    now = _now()
    last_refresh = _LANGUAGE_REFRESH_INFLIGHT.get(refresh_key, 0.0)
    if now - last_refresh < 20.0:
        return
    _LANGUAGE_REFRESH_INFLIGHT[refresh_key] = now

    try:
        chapters_payload = await get_chapter_list(title_id, "")
        lang = _user_lang(user_id)
        bundle = (
            get_cached_title_bundle(title_id, lang)
            or get_cached_title_overview(title_id)
            or _fallback_title_bundle(title_id)
        )
        if not isinstance(bundle, dict):
            bundle = _fallback_title_bundle(title_id)

        bundle = {
            **bundle,
            "title_id": str(bundle.get("title_id") or title_id).strip(),
            "chapters": chapters_payload.get("chapters") or bundle.get("chapters") or [],
            "languages": chapters_payload.get("languages") or bundle.get("languages") or [],
            "chapters_partial": bool(chapters_payload.get("partial")),
            "chapters_error": chapters_payload.get("error") or bundle.get("chapters_error") or "",
        }
        bundle["languages_loading"] = not bool(bundle_language_options(bundle, include_default=False))

        async with _message_lock(chat_id, message_id):
            kind, ref = _get_panel_state(chat_id, message_id)
            bundle_title_id = str(bundle.get("title_id") or title_id).strip()
            if kind != "language" or ref != bundle_title_id:
                return

            await _render_panel_to_message(
                context,
                chat_id=chat_id,
                message_id=message_id,
                text=_language_text(bundle, user_id),
                keyboard=_language_keyboard(bundle, user_id),
                photo=_pick_bundle_image(bundle),
            )
    except Exception as error:
        print("[LANGUAGE_PANEL][BACKGROUND_ERROR]", title_id, repr(error))
    finally:
        _LANGUAGE_REFRESH_INFLIGHT.pop(refresh_key, None)


async def send_title_panel(target, context: ContextTypes.DEFAULT_TYPE, title_id: str, user_id: int | None, *, edit: bool):
    lang = _user_lang(user_id)
    bundle = await _load_title_panel_bundle(title_id, lang)

    refresh_tasks = []
    if bundle.get("chapters_partial") or not bundle.get("chapters"):
        refresh_tasks.append(fire_and_forget(get_title_chapters_snapshot(title_id, lang)))
    if bundle.get("chapters_partial") or not bundle.get("chapters") or bundle.get("metadata_partial"):
        refresh_tasks.append(fire_and_forget(get_title_bundle(title_id, lang)))

    last_read = await run_sync(get_last_read_entry, user_id, bundle["title_id"]) if user_id else None

    chapters = flatten_chapters({"chapters": bundle.get("chapters") or []}, lang)
    if bundle.get("chapters"):
        latest = _latest_chapter_dict(bundle)
        chapter_ids = [latest.get("chapter_id") or ""]
        chapter_ids.extend(item.get("chapter_id") or "" for item in chapters[:3])
        prefetch_reader_payloads(chapter_ids, limit=4)

    if user_id:
        fire_and_forget_sync(
            log_event,
            event_type="title_open",
            user_id=user_id,
            title_id=bundle["title_id"],
            title_name=bundle.get("title") or "",
        )

    panel_message = await _render_panel(
        target,
        _title_text(bundle, last_read, user_id),
        _title_keyboard(bundle, last_read, user_id),
        _pick_bundle_image(bundle),
        edit=edit,
    )
    if panel_message:
        _set_panel_state(panel_message.chat.id, panel_message.message_id, "title", bundle["title_id"])
        for refresh_task in refresh_tasks:
            fire_and_forget(_auto_finalize_title_panel(context, panel_message, bundle["title_id"], user_id, refresh_task))


async def send_language_panel(target, context: ContextTypes.DEFAULT_TYPE, title_id: str, user_id: int | None, *, edit: bool):
    lang = _user_lang(user_id)
    bundle = await _load_title_panel_bundle(title_id, lang)
    cached_chapters = _get_cached_chapter_list_compat(title_id, "")
    if cached_chapters and (cached_chapters.get("chapters") or cached_chapters.get("languages")):
        bundle = {
            **bundle,
            "chapters": cached_chapters.get("chapters") or bundle.get("chapters") or [],
            "languages": cached_chapters.get("languages") or bundle.get("languages") or [],
            "chapters_partial": False,
        }

    needs_full_language_scan = (
        len(bundle_language_options(bundle, include_default=False)) <= 1
        or bool(bundle.get("chapters_partial"))
    )

    if needs_full_language_scan:
        try:
            full_chapters = await asyncio.wait_for(get_chapter_list_fast(title_id, ""), timeout=2.5)
            if full_chapters.get("chapters") or full_chapters.get("languages"):
                bundle = {
                    **bundle,
                    "chapters": full_chapters.get("chapters") or bundle.get("chapters") or [],
                    "languages": full_chapters.get("languages") or bundle.get("languages") or [],
                    "chapters_partial": bool(full_chapters.get("partial")),
                    "chapters_error": full_chapters.get("error") or bundle.get("chapters_error") or "",
                }
        except Exception as error:
            print("[LANGUAGE_PANEL][FAST_SCAN_FAIL]", title_id, repr(error))
            bundle = {**bundle, "languages_loading": True}

    if len(bundle_language_options(bundle, include_default=False)) == 0:
        bundle = {**bundle, "languages_loading": True}

    panel_message = await _render_panel(
        target,
        _language_text(bundle, user_id),
        _language_keyboard(bundle, user_id),
        _pick_bundle_image(bundle),
        edit=edit,
    )
    if panel_message:
        _set_panel_state(panel_message.chat.id, panel_message.message_id, "language", bundle["title_id"])
        if bundle.get("languages_loading") or needs_full_language_scan:
            fire_and_forget(_auto_finalize_language_panel(context, panel_message, bundle["title_id"], user_id))


async def _load_offline_bundle(title_id: str, lang: str | None = None) -> tuple[dict, list[asyncio.Task]]:
    resolved_lang = normalize_language(lang) or PREFERRED_CHAPTER_LANG
    bundle = get_cached_title_bundle(title_id, resolved_lang)
    if bundle is None:
        try:
            bundle = await asyncio.wait_for(get_title_chapters_snapshot(title_id, resolved_lang), timeout=4.8)
        except Exception as error:
            print("[OFFLINE_CHAPTERS][FAST_FALLBACK]", title_id, repr(error))
            bundle = _fallback_title_bundle(title_id)

    refresh_tasks = []
    if bundle.get("chapters_partial") or not bundle.get("chapters"):
        refresh_tasks.append(fire_and_forget(get_title_chapters_snapshot(title_id, resolved_lang)))
        refresh_tasks.append(fire_and_forget(get_title_bundle(title_id, resolved_lang)))
    elif bundle.get("metadata_partial"):
        refresh_tasks.append(fire_and_forget(get_title_bundle(title_id, resolved_lang)))

    return bundle, refresh_tasks


async def send_offline_chapters_page(
    target,
    context: ContextTypes.DEFAULT_TYPE,
    title_id: str,
    page: int,
    user_id: int | None,
    *,
    edit: bool,
):
    lang = _user_lang(user_id)
    bundle, refresh_tasks = await _load_offline_bundle(title_id, lang)

    if not can_use_pdf_bulk(user_id):
        await _send_offline_locked(target, bundle, user_id)
        return

    chapters = flatten_chapters({"chapters": bundle.get("chapters") or []}, lang)
    total_pages = max(1, ((len(chapters) - 1) // CHAPTERS_PER_PAGE) + 1)
    page = max(1, min(int(page or 1), total_pages))

    panel_message = await _render_panel(
        target,
        _offline_chapters_text(bundle, page, len(chapters)),
        _offline_chapters_keyboard(bundle, chapters, page),
        _pick_bundle_image(bundle),
        edit=edit,
    )
    if panel_message:
        _set_panel_state(panel_message.chat.id, panel_message.message_id, "offline", bundle["title_id"])
        for refresh_task in refresh_tasks:
            fire_and_forget(_auto_finalize_offline_panel(context, panel_message, bundle["title_id"], user_id, refresh_task))


async def send_offline_panel(target, context: ContextTypes.DEFAULT_TYPE, title_id: str, user_id: int | None, *, edit: bool):
    await send_offline_chapters_page(target, context, title_id, 1, user_id, edit=edit)


async def send_offline_bulk_panel(target, context: ContextTypes.DEFAULT_TYPE, title_id: str, user_id: int | None, *, edit: bool):
    lang = _user_lang(user_id)
    bundle = get_cached_title_bundle(title_id, lang) or _fallback_title_bundle(title_id)

    if not can_use_pdf_bulk(user_id):
        await _send_offline_locked(target, bundle, user_id)
        return

    panel_message = await _render_panel(
        target,
        _offline_text(bundle),
        _offline_keyboard(bundle),
        _pick_bundle_image(bundle),
        edit=edit,
    )
    if panel_message:
        _set_panel_state(panel_message.chat.id, panel_message.message_id, "offline_bulk", bundle["title_id"])


async def verify_offline_payment_panel(target, context: ContextTypes.DEFAULT_TYPE, title_id: str, user_id: int):
    if not cakto_api_configured():
        await _send_offline_locked(target, await get_title_bundle(title_id, _user_lang(user_id)), user_id)
        message = getattr(target, "message", None)
        try:
            await (message.reply_text if message else target.reply_text)(
                (
                    "⚠️ <b>Não consegui verificar pela API da Cakto.</b>\n\n"
                    "O bot ainda precisa de <code>CAKTO_CLIENT_ID</code> e "
                    "<code>CAKTO_CLIENT_SECRET</code> no ambiente para consultar pedidos.\n\n"
                    "Se você já pagou, chame o suporte para a liberação manual."
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛟 Suporte", url=SUPPORT_BOT_URL)]]),
            )
        except Exception:
            pass
        return

    try:
        result = await verify_cakto_payment_for_user(user_id)
    except Exception:
        result = {"ok": False, "reason": "api_error"}

    if result.get("ok"):
        await send_offline_panel(target, context, title_id, user_id, edit=True)
        return

    reason = result.get("reason") or "not_found"
    if reason == "not_found":
        text = "Ainda não encontrei uma compra aprovada vinculada ao seu Telegram."
    elif reason == "order_not_paid":
        text = "Encontrei seu pedido, mas ele ainda não aparece como pago na Cakto."
    else:
        text = "Não consegui confirmar o pagamento agora."

    message = getattr(target, "message", None)
    try:
        await (message.reply_text if message else target.reply_text)(
            f"⏳ <b>Pagamento ainda não confirmado</b>\n\n{text}\n\n"
            "Se o Pix já saiu da sua conta, fale com o suporte para conferirmos manualmente.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛟 Suporte", url=SUPPORT_BOT_URL)]]),
        )
    except Exception:
        pass


async def send_chapters_page(target, context: ContextTypes.DEFAULT_TYPE, title_id: str, page: int, user_id: int | None, *, edit: bool):
    lang = _user_lang(user_id)
    bundle = get_cached_title_bundle(title_id, lang)
    if bundle is None or not bundle.get("chapters"):
        try:
            bundle = await asyncio.wait_for(get_title_chapters_snapshot(title_id, lang), timeout=START_TITLE_RESOLVE_TIMEOUT)
        except Exception as error:
            print("[CHAPTER_PANEL][SNAPSHOT_FALLBACK]", title_id, repr(error))
            try:
                bundle = await asyncio.wait_for(get_title_bundle(title_id, lang), timeout=CHAPTER_PANEL_FAST_TIMEOUT)
            except Exception as bundle_error:
                print("[CHAPTER_PANEL][FAST_FALLBACK]", title_id, repr(bundle_error))
                bundle = None

    if bundle is None:
        prefetch_title_bundles([title_id], lang=lang, limit=1)
        bundle = _fallback_title_bundle(title_id)

    chapters = flatten_chapters({"chapters": bundle.get("chapters") or []}, lang)
    read_ids = set(await run_sync(get_read_chapter_ids, user_id, bundle["title_id"])) if user_id else set()

    total_pages = max(1, ((len(chapters) - 1) // CHAPTERS_PER_PAGE) + 1)
    page = max(1, min(page, total_pages))

    start = (page - 1) * CHAPTERS_PER_PAGE
    end = min(start + CHAPTERS_PER_PAGE, len(chapters))
    visible_ids = [(item.get("chapter_id") or "") for item in chapters[start:end]]
    if visible_ids:
        prefetch_reader_payloads(visible_ids, limit=len(visible_ids))

    panel_message = await _render_panel(
        target,
        _chapter_list_text(bundle, page, len(chapters), lang),
        _chapter_list_keyboard(bundle, chapters, page, read_ids, lang, user_id),
        _pick_bundle_image(bundle),
        edit=edit,
    )
    if panel_message:
        _set_panel_state(panel_message.chat.id, panel_message.message_id, "chapters", bundle["title_id"])


async def send_chapter_panel(target, context: ContextTypes.DEFAULT_TYPE, chapter_id: str, user_id: int | None, *, edit: bool):
    lang = _user_lang(user_id)
    chapter = get_cached_chapter_reader_payload(chapter_id, lang) or await get_chapter_reader_payload(chapter_id, lang)

    adjacent_refs = [
        (chapter.get("previous_chapter") or {}).get("chapter_id") or "",
        (chapter.get("next_chapter") or {}).get("chapter_id") or "",
    ]
    prefetch_reader_payloads(adjacent_refs, lang=lang, limit=2)

    fire_and_forget(get_title_bundle(chapter["title_id"], lang))

    if user_id:
        fire_and_forget_sync(
            mark_chapter_read,
            user_id=user_id,
            title_id=chapter["title_id"],
            chapter_id=chapter["chapter_id"],
            chapter_number=chapter.get("chapter_number") or "",
            title_name=chapter.get("title") or "",
            chapter_url="",
        )
        fire_and_forget_sync(
            log_event,
            event_type="chapter_open",
            user_id=user_id,
            title_id=chapter["title_id"],
            title_name=chapter.get("title") or "",
            chapter_id=chapter["chapter_id"],
            chapter_number=chapter.get("chapter_number") or "",
        )

    telegraph_url = get_cached_chapter_page_url(chapter["chapter_id"])
    telegraph_task: asyncio.Task | None = None

    if not telegraph_url:
        telegraph_task = asyncio.create_task(
            get_or_create_chapter_page(
                chapter_id=chapter["chapter_id"],
                title=_chapter_telegraph_title(chapter),
                images=chapter.get("images") or [],
            )
        )
        try:
            telegraph_url = await asyncio.wait_for(asyncio.shield(telegraph_task), timeout=TELEGRAPH_INLINE_WAIT)
        except asyncio.TimeoutError:
            telegraph_url = ""
        except Exception:
            telegraph_task = None

    panel_message = await _render_panel(
        target,
        _chapter_text(chapter),
        _chapter_keyboard(chapter, telegraph_url=telegraph_url, telegraph_pending=not bool(telegraph_url), user_id=user_id),
        _pick_chapter_image(chapter),
        edit=edit,
    )
    if panel_message:
        _set_panel_state(panel_message.chat.id, panel_message.message_id, "chapter", chapter["chapter_id"])

    if panel_message and not telegraph_url and telegraph_task is not None:
        fire_and_forget(_auto_finalize_telegraph_panel(context, panel_message, chapter, telegraph_task))


async def send_offline_chapter_panel(
    target,
    context: ContextTypes.DEFAULT_TYPE,
    chapter_id: str,
    user_id: int | None,
    *,
    page: int = 1,
    edit: bool,
):
    if not can_use_pdf_bulk(user_id):
        await _safe_answer_query(target, "Função liberada só para assinantes.", show_alert=True)
        return

    lang = _user_lang(user_id)
    chapter = get_cached_chapter_reader_payload(chapter_id, lang) or await get_chapter_reader_payload(chapter_id, lang)
    adjacent_refs = [
        (chapter.get("previous_chapter") or {}).get("chapter_id") or "",
        (chapter.get("next_chapter") or {}).get("chapter_id") or "",
    ]
    prefetch_reader_payloads(adjacent_refs, lang=lang, limit=2)
    fire_and_forget(get_title_bundle(chapter["title_id"], lang))

    panel_message = await _render_panel(
        target,
        _offline_chapter_text(chapter),
        _offline_chapter_keyboard(chapter, page),
        _pick_chapter_image(chapter),
        edit=edit,
    )
    if panel_message:
        _set_panel_state(panel_message.chat.id, panel_message.message_id, "offline_chapter", chapter["chapter_id"])


async def _send_telegraph(query, chapter_id: str):
    lang = _user_lang(getattr(getattr(query, "from_user", None), "id", None))
    chapter = get_cached_chapter_reader_payload(chapter_id, lang) or await get_chapter_reader_payload(chapter_id, lang)
    url = await get_or_create_chapter_page(
        chapter_id=chapter["chapter_id"],
        title=_chapter_telegraph_title(chapter),
        images=chapter.get("images") or [],
    )
    panel_message = await _render_panel(
        query,
        _chapter_text(chapter),
        _chapter_keyboard(
            chapter,
            telegraph_url=url,
            user_id=getattr(getattr(query, "from_user", None), "id", None),
        ),
        _pick_chapter_image(chapter),
        edit=True,
    )
    if panel_message:
        _set_panel_state(panel_message.chat.id, panel_message.message_id, "chapter", chapter["chapter_id"])


async def _enqueue_pdf(query, context: ContextTypes.DEFAULT_TYPE, chapter_id: str):
    lang = _user_lang(getattr(getattr(query, "from_user", None), "id", None))
    chapter = get_cached_chapter_reader_payload(chapter_id, lang) or await get_chapter_reader_payload(chapter_id, lang)
    await enqueue_pdf_job(
        context.application,
        PdfJob(
            chat_id=query.message.chat_id,
            chapter_id=chapter["chapter_id"],
            chapter_number=chapter.get("chapter_number") or "",
            title_name=chapter.get("title") or "Manga",
            images=chapter.get("images") or [],
            caption=(
                f"📄 <b>{html.escape(chapter.get('title') or 'Manga')}</b>\n"
                f"Capítulo <code>{html.escape(chapter.get('chapter_number') or '?')}</code>\n"
                "@MangasBrasil"
            ),
        ),
    )
    return chapter


async def _enqueue_epub(query, context: ContextTypes.DEFAULT_TYPE, chapter_id: str):
    lang = _user_lang(getattr(getattr(query, "from_user", None), "id", None))
    chapter = get_cached_chapter_reader_payload(chapter_id, lang) or await get_chapter_reader_payload(chapter_id, lang)
    tag = html.escape(DISTRIBUTION_TAG or "")
    await enqueue_epub_job(
        context.application,
        EpubJob(
            chat_id=query.message.chat_id,
            chapter_id=chapter["chapter_id"],
            chapter_number=chapter.get("chapter_number") or "",
            title_name=chapter.get("title") or "Manga",
            images=chapter.get("images") or [],
            caption=(
                f"📚 <b>{html.escape(chapter.get('title') or 'Manga')}</b>\n"
                f"Capítulo <code>{html.escape(chapter.get('chapter_number') or '?')}</code>\n"
                f"{tag}"
            ).strip(),
        ),
    )
    return chapter


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not query or not query.data:
        return
    if not query.data.startswith("mb|"):
        return

    if await handle_language_callback(update, context):
        return

    if query.data == "mb|noop":
        await _safe_answer_query(query)
        return

    if not user:
        await _safe_answer_query(query, "Não consegui identificar seu usuário agora.", show_alert=True)
        return

    if _is_callback_cooldown(context, user.id, query.data):
        await _safe_answer_query(query, "⏳ Aguarde um instante antes de apertar de novo.", show_alert=False)
        return

    message = query.message
    user_lock = _user_lock(user.id)
    msg_lock = _message_lock(message.chat.id, message.message_id) if message else asyncio.Lock()

    current_action = _action_signature(query.data)
    if message and _get_inflight_action(message.chat.id, message.message_id) == current_action:
        await _safe_answer_query(query, "⏳ Essa ação já está sendo processada.", show_alert=False)
        return

    parts = query.data.split("|")
    action = parts[1] if len(parts) > 1 else ""
    user_id = user.id
    original_reply_markup = getattr(message, "reply_markup", None)

    async with user_lock:
        async with msg_lock:
            if message:
                if _get_inflight_action(message.chat.id, message.message_id) == current_action:
                    await _safe_answer_query(query, "⏳ Essa ação já está sendo processada.", show_alert=False)
                    return
                _set_inflight_action(message.chat.id, message.message_id, current_action)

            try:
                if action == "stopbulk" and len(parts) >= 3:
                    stopped = await stop_pdf_bulk(context, job_id=parts[2], user_id=user_id)
                    if stopped:
                        await _safe_answer_query(query, "Parando o download offline.", show_alert=False)
                    else:
                        await _safe_answer_query(query, "Esse lote já terminou ou expirou.", show_alert=True)
                    return

                if action == "title" and len(parts) >= 3:
                    await _safe_answer_query(query)
                    await _show_loading_markup(query, "⏳ Abrindo obra")
                    await send_title_panel(query, context, parts[2], user_id, edit=True)
                    return

                if action == "plan":
                    await _safe_answer_query(query)
                    await send_plan_panel(query, user_id)
                    return

                if action == "home":
                    await _safe_answer_query(query)
                    from handlers.start import edit_home_panel
                    await edit_home_panel(query, user_id, user.first_name or "leitor")
                    return

                if action == "lang" and len(parts) >= 3:
                    await _safe_answer_query(query)
                    await send_language_panel(query, context, parts[2], user_id, edit=True)
                    return

                if action == "setlang" and len(parts) >= 4:
                    lang = normalize_language(parts[3])
                    if not lang:
                        await _safe_answer_query(query, "Idioma inválido.", show_alert=True)
                        return
                    set_user_language(user_id, lang)
                    await _safe_answer_query(query, f"Idioma alterado para {language_badge(lang)}.", show_alert=False)
                    await _show_loading_markup(query, "⏳ Atualizando obra")
                    await send_title_panel(query, context, parts[2], user_id, edit=True)
                    return

                if action == "offline" and len(parts) >= 3:
                    await _safe_answer_query(query)
                    if can_use_pdf_bulk(user_id):
                        await _show_loading_markup(query, "⏳ Carregando offline")
                    await send_offline_panel(query, context, parts[2], user_id, edit=True)
                    return

                if action == "offchap" and len(parts) >= 4:
                    if not can_use_pdf_bulk(user_id):
                        await _safe_answer_query(query, "Função liberada só para assinantes.", show_alert=True)
                        return
                    await _safe_answer_query(query)
                    await _show_loading_markup(query, "⏳ Carregando capítulos")
                    await send_offline_chapters_page(query, context, parts[2], int(parts[3]), user_id, edit=True)
                    return

                if action == "offbulk" and len(parts) >= 3:
                    if not can_use_pdf_bulk(user_id):
                        await _safe_answer_query(query, "Função liberada só para assinantes.", show_alert=True)
                        return
                    await _safe_answer_query(query)
                    await _show_loading_markup(query, "⏳ Opções de lote")
                    await send_offline_bulk_panel(query, context, parts[2], user_id, edit=True)
                    return

                if action == "paycheck" and len(parts) >= 3:
                    await _safe_answer_query(query, "Verificando pagamento na Cakto...", show_alert=False)
                    await _show_loading_markup(query, "⏳ Verificando pagamento", reply_markup=_payment_check_keyboard())
                    await verify_offline_payment_panel(query, context, parts[2], user_id)
                    return

                if action == "chap" and len(parts) >= 4:
                    await _safe_answer_query(query)
                    await _show_loading_markup(query, "⏳ Carregando capítulos")
                    await send_chapters_page(query, context, parts[2], int(parts[3]), user_id, edit=True)
                    return

                if action == "read" and len(parts) >= 3:
                    await _safe_answer_query(query)
                    await _show_loading_markup(query, "⏳ Abrindo capítulo")
                    await send_chapter_panel(query, context, parts[2], user_id, edit=True)
                    return

                if action == "offread" and len(parts) >= 3:
                    if not can_use_pdf_bulk(user_id):
                        await _safe_answer_query(query, "Função liberada só para assinantes.", show_alert=True)
                        return
                    await _safe_answer_query(query)
                    await _show_loading_markup(query, "⏳ Abrindo capítulo offline")
                    await send_offline_chapter_panel(
                        query,
                        context,
                        parts[2],
                        user_id,
                        page=int(parts[3]) if len(parts) >= 4 else 1,
                        edit=True,
                    )
                    return

                if action == "sp" and len(parts) >= 4:
                    await _safe_answer_query(query)
                    await _show_loading_markup(query, "⏳ Carregando página")
                    rendered = render_search_page(context, parts[2], int(parts[3]))
                    if not rendered:
                        await _safe_answer_query(query, "Essa busca expirou. Faz outra busca pra continuar.", show_alert=True)
                        return
                    await edit_search_page(query, rendered)
                    return

                if action == "tg" and len(parts) >= 3:
                    await _safe_answer_query(query)
                    await _show_loading_markup(query, "⏳ Preparando leitura rápida")
                    await _send_telegraph(query, parts[2])
                    return

                if action == "pdfall" and len(parts) >= 3:
                    if not can_use_pdf_bulk(user_id):
                        await _safe_answer_query(query, "Função liberada só para assinantes.", show_alert=True)
                        return
                    await _safe_answer_query(query, "Pedido recebido. Vou preparar os PDFs.", show_alert=False)
                    await request_pdf_bulk_for_title(
                        context,
                        chat_id=message.chat.id if message else user_id,
                        user_id=user_id,
                        title_ref=parts[2],
                        order=normalize_pdf_bulk_order(parts[3] if len(parts) >= 4 else "asc"),
                        lang=_user_lang(user_id),
                    )
                    return

                if action == "pdf" and len(parts) >= 3:
                    await _safe_answer_query(query)
                    await _show_loading_markup(query, "⏳ Preparando PDF")
                    chapter = await _enqueue_pdf(query, context, parts[2])
                    await _render_panel(
                        query,
                        _chapter_text(chapter),
                        _chapter_keyboard(
                            chapter,
                            telegraph_url=get_cached_chapter_page_url(chapter["chapter_id"]),
                            telegraph_pending=not bool(get_cached_chapter_page_url(chapter["chapter_id"])),
                            user_id=user_id,
                        ),
                        _pick_chapter_image(chapter),
                        edit=True,
                    )
                    return

                if action == "offpdf" and len(parts) >= 3:
                    if not can_use_pdf_bulk(user_id):
                        await _safe_answer_query(query, "Função liberada só para assinantes.", show_alert=True)
                        return
                    await _safe_answer_query(query, "PDF enviado para a fila.", show_alert=False)
                    await _show_loading_markup(query, "⏳ Preparando PDF")
                    chapter = await _enqueue_pdf(query, context, parts[2])
                    page = int(parts[3]) if len(parts) >= 4 else 1
                    await _render_panel(
                        query,
                        _offline_chapter_text(chapter),
                        _offline_chapter_keyboard(chapter, page),
                        _pick_chapter_image(chapter),
                        edit=True,
                    )
                    return

                if action == "offepub" and len(parts) >= 3:
                    if not can_use_pdf_bulk(user_id):
                        await _safe_answer_query(query, "Função liberada só para assinantes.", show_alert=True)
                        return
                    await _safe_answer_query(query, "EPUB enviado para a fila.", show_alert=False)
                    await _show_loading_markup(query, "⏳ Preparando EPUB")
                    chapter = await _enqueue_epub(query, context, parts[2])
                    page = int(parts[3]) if len(parts) >= 4 else 1
                    await _render_panel(
                        query,
                        _offline_chapter_text(chapter),
                        _offline_chapter_keyboard(chapter, page),
                        _pick_chapter_image(chapter),
                        edit=True,
                    )
                    return

                await _safe_answer_query(query, "Ação inválida ou expirada.", show_alert=False)

            except ValueError:
                await _safe_answer_query(query, "Dados inválidos nessa ação.", show_alert=True)
                await _restore_reply_markup(query, original_reply_markup)
            except Exception:
                await _safe_answer_query(query, "Ocorreu um erro ao processar essa ação.", show_alert=True)
                await _restore_reply_markup(query, original_reply_markup)
            finally:
                if message:
                    _clear_inflight_action(message.chat.id, message.message_id)
