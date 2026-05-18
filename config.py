import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_local_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    try:
        raw_lines = env_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return

    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


_load_local_env()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _env_int_list(name: str) -> list[int]:
    raw = os.getenv(name, "").replace(";", ",")
    values: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if item.isdigit():
            values.append(int(item))
    return values


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on", "sim", "s"}:
        return True
    if raw in {"0", "false", "no", "off", "nao", "não", "n"}:
        return False
    return default


def _env_str_list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, "").strip() or default
    raw = raw.replace(";", ",")
    return [item.strip() for item in raw.split(",") if item.strip()]


BOT_TOKEN = os.getenv("BOT_TOKEN", "8719336176:AAGsY1XJ4yJqlM5wOIZdkDVImVzN6K6bEYw").strip()
CATALOG_SITE_BASE = (
    os.getenv("CATALOG_SITE_BASE", "https://mangaball.net").strip()
    or os.getenv("SOURCE_SITE_BASE", "https://mangaball.net").strip()
).rstrip("/")
CATALOG_COOKIE_HEADER = os.getenv("CATALOG_COOKIE_HEADER", "").strip()
CATALOG_USER_AGENT = os.getenv("CATALOG_USER_AGENT", "").strip()

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@MangasBrasil").strip()
REQUIRED_CHANNELS = _env_str_list(
    "REQUIRED_CHANNELS",
    "@AtualizacoesOn,@MangasBrasil,@QG_BALTIGO",
)
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "https://t.me/MangasBrasil").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "MangasBaltigo_Bot").strip().lstrip("@")
BOT_BRAND = os.getenv("BOT_BRAND", "Mangas Baltigo").strip()
WEBAPP_BASE_URL = os.getenv("WEBAPP_BASE_URL", "").strip().rstrip("/")
CANAL_POSTAGEM = os.getenv("CANAL_POSTAGEM", "@MangasBrasil").strip()
CANAL_POSTAGEM_MANGA = (
    os.getenv("CANAL_POSTAGEM_MANGA", "@MangasBrasil").strip()
    or os.getenv("POSTMANGA_CHANNEL", "@MangasBrasil").strip()
    or CANAL_POSTAGEM
)
CANAL_POSTAGEM_CAPITULOS = (
    os.getenv("CANAL_POSTAGEM_CAPITULOS", "@AtualizacoesOn").strip()
    or os.getenv("AUTO_POST_CHANNEL", "@AtualizacoesOn").strip()
    or CANAL_POSTAGEM
)

ADMIN_IDS = [
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "1852596083").split(",")
    if value.strip().isdigit()
]

SEARCH_LIMIT = _env_int("SEARCH_LIMIT", 10)
CHAPTERS_PER_PAGE = _env_int("CHAPTERS_PER_PAGE", 15)
EPISODES_PER_PAGE = CHAPTERS_PER_PAGE
ANTI_FLOOD_SECONDS = _env_float("ANTI_FLOOD_SECONDS", 1.0)
API_CACHE_TTL_SECONDS = _env_int("API_CACHE_TTL_SECONDS", 900)
HTTP_TIMEOUT = _env_int("HTTP_TIMEOUT", 40)
HOME_SECTION_LIMIT = _env_int("HOME_SECTION_LIMIT", 12)
AUTO_POST_LIMIT = _env_int("AUTO_POST_LIMIT", 6)
PREFERRED_CHAPTER_LANG = os.getenv("PREFERRED_CHAPTER_LANG", "pt-br").strip().lower()
RECENT_CHAPTER_TIME = os.getenv("RECENT_CHAPTER_TIME", "week").strip().lower() or "week"
ANILIST_API_URL = os.getenv("ANILIST_API_URL", "https://graphql.anilist.co").strip()
ANILIST_CACHE_TTL_SECONDS = _env_int("ANILIST_CACHE_TTL_SECONDS", 21600)

PDF_CACHE_DIR = str(DATA_DIR / "pdf_cache")
EPUB_CACHE_DIR = str(DATA_DIR / "epub_cache")
IMAGE_CACHE_ORIGINAL_MAX_MB = _env_int("IMAGE_CACHE_ORIGINAL_MAX_MB", 2048)
IMAGE_CACHE_TELEGRAPH_MAX_MB = _env_int("IMAGE_CACHE_TELEGRAPH_MAX_MB", 2048)
PDF_CACHE_MAX_MB = _env_int("PDF_CACHE_MAX_MB", 1024)
EPUB_CACHE_MAX_MB = _env_int("EPUB_CACHE_MAX_MB", 1024)
CACHE_CLEANUP_ENABLED = _env_bool("CACHE_CLEANUP_ENABLED", True)
CACHE_CLEANUP_STARTUP = _env_bool("CACHE_CLEANUP_STARTUP", True)
CACHE_CLEANUP_INTERVAL_SECONDS = _env_int("CACHE_CLEANUP_INTERVAL_SECONDS", 1800)
CACHE_CLEANUP_MIN_ENTRY_AGE_MINUTES = _env_int("CACHE_CLEANUP_MIN_ENTRY_AGE_MINUTES", 10)
IMAGE_CACHE_ORIGINAL_MAX_AGE_HOURS = _env_int("IMAGE_CACHE_ORIGINAL_MAX_AGE_HOURS", 12)
IMAGE_CACHE_TELEGRAPH_MAX_AGE_HOURS = _env_int("IMAGE_CACHE_TELEGRAPH_MAX_AGE_HOURS", 24)
PDF_CACHE_MAX_AGE_HOURS = _env_int("PDF_CACHE_MAX_AGE_HOURS", 24)
EPUB_CACHE_MAX_AGE_HOURS = _env_int("EPUB_CACHE_MAX_AGE_HOURS", 24)
PDF_NAME_PATTERN = os.getenv(
    "PDF_NAME_PATTERN",
    "{title} - Capitulo {chapter}.pdf",
).strip()
EPUB_NAME_PATTERN = os.getenv(
    "EPUB_NAME_PATTERN",
    "{title} - Capitulo {chapter}.epub",
).strip()
PDF_QUEUE_LIMIT = _env_int("PDF_QUEUE_LIMIT", 30)
EPUB_QUEUE_LIMIT = _env_int("EPUB_QUEUE_LIMIT", PDF_QUEUE_LIMIT)
PDF_WORKERS_SINGLE = _env_int("PDF_WORKERS_SINGLE", 1)
PDF_WORKERS_BULK = _env_int("PDF_WORKERS_BULK", 1)
EPUB_WORKERS = _env_int("EPUB_WORKERS", 1)
PDF_PROTECT_CONTENT = _env_bool("PDF_PROTECT_CONTENT", True)
DOCUMENT_ARCHIVE_CHANNEL = os.getenv("DOCUMENT_ARCHIVE_CHANNEL", "-1003722313865").strip()
PDF_BULK_ALLOWED_IDS = sorted(set(ADMIN_IDS + _env_int_list("PDF_BULK_ALLOWED_IDS")))
PDF_BULK_MAX_CHAPTERS = _env_int("PDF_BULK_MAX_CHAPTERS", 0)
PDF_BULK_DELAY_SECONDS = _env_float("PDF_BULK_DELAY_SECONDS", 0.2)
PDF_BULK_SUBSCRIBE_URL = os.getenv("PDF_BULK_SUBSCRIBE_URL", REQUIRED_CHANNEL_URL).strip()
CAKTO_WEBHOOK_SECRET = os.getenv("CAKTO_WEBHOOK_SECRET", "").strip()
CAKTO_REQUIRE_WEBHOOK_SECRET = _env_bool("CAKTO_REQUIRE_WEBHOOK_SECRET", bool(CAKTO_WEBHOOK_SECRET))
CAKTO_NOTIFY_USERS = _env_bool("CAKTO_NOTIFY_USERS", True)
CAKTO_CLIENT_ID = os.getenv("CAKTO_CLIENT_ID", "").strip()
CAKTO_CLIENT_SECRET = os.getenv("CAKTO_CLIENT_SECRET", "").strip()
CAKTO_API_BASE_URL = os.getenv("CAKTO_API_BASE_URL", "https://api.cakto.com.br").strip().rstrip("/")
CAKTO_ORDER_SYNC_LIMIT = _env_int("CAKTO_ORDER_SYNC_LIMIT", 100)
CAKTO_PLAN_BRONZE_URL = (
    os.getenv("CAKTO_PLAN_BRONZE_URL", "").strip()
    or os.getenv("CAKTO_PLAN_WEEKLY_URL", "").strip()
    or ""
)
CAKTO_PLAN_OURO_URL = (
    os.getenv("CAKTO_PLAN_OURO_URL", "").strip()
    or os.getenv("CAKTO_PLAN_MONTHLY_URL", "").strip()
    or ""
)
CAKTO_PLAN_DIAMANTE_URL = (
    os.getenv("CAKTO_PLAN_DIAMANTE_URL", "").strip()
    or os.getenv("CAKTO_PLAN_ANNUAL_URL", "").strip()
    or os.getenv("CAKTO_PLAN_12M_URL", "").strip()
    or ""
)
CAKTO_PLAN_RUBI_URL = (
    os.getenv("CAKTO_PLAN_RUBI_URL", "").strip()
    or ""
)
CAKTO_PLAN_1M_URL = CAKTO_PLAN_OURO_URL
CAKTO_PLAN_3M_URL = os.getenv("CAKTO_PLAN_3M_URL", "").strip()
CAKTO_PLAN_6M_URL = os.getenv("CAKTO_PLAN_6M_URL", "").strip()
CAKTO_PLAN_LIFETIME_URL = CAKTO_PLAN_RUBI_URL
TELEGRAPH_AUTHOR = os.getenv("TELEGRAPH_AUTHOR", BOT_BRAND).strip() or BOT_BRAND
STICKER_DIVISOR = os.getenv("STICKER_DIVISOR", "").strip()
PROMO_BANNER_URL = os.getenv(
    "PROMO_BANNER_URL",
    "https://photo.chelpbot.me/AgACAgEAAxkBa-uhxmn-Ynl49i4sc-QGdecUy6MivtqbAAJlDGsbr6GIR8U7RAEQqdGuAQADAgADeQADOwQ/photo.jpg",
).strip()
BALTIGO_UNIVERSE_WEBAPP_URL = os.getenv(
    "BALTIGO_UNIVERSE_WEBAPP_URL",
    "https://rough-double-remarkable-north.trycloudflare.com/miniapp/bots/index.html",
).strip()
DISTRIBUTION_TAG = os.getenv("DISTRIBUTION_TAG", "@MangasBrasil").strip() or "@MangasBrasil"
API_CACHE_MAX_ENTRIES = _env_int("API_CACHE_MAX_ENTRIES", 120)
API_RATE_LIMIT_PER_MINUTE = _env_int("API_RATE_LIMIT_PER_MINUTE", 120)
WEBAPP_CORS_ORIGINS = _env_str_list("WEBAPP_CORS_ORIGINS", "")
WEBAPP_TRUST_QUERY_USER_ID = _env_bool("WEBAPP_TRUST_QUERY_USER_ID", True)

AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_API_URL = os.getenv(
    "AI_API_URL",
    "https://api.openai.com/v1/chat/completions",
).strip()
AI_MODEL = os.getenv("AI_MODEL", "").strip()
AI_REQUEST_TIMEOUT = _env_int("AI_REQUEST_TIMEOUT", 25)
AI_MAX_OUTPUT_TOKENS = _env_int("AI_MAX_OUTPUT_TOKENS", 180)
AI_TEMPERATURE = _env_float("AI_TEMPERATURE", 0.7)
AI_DEFAULT_CHANCE = _env_int("AI_DEFAULT_CHANCE", 5)
AI_DEFAULT_COOLDOWN_MINUTES = _env_int("AI_DEFAULT_COOLDOWN_MINUTES", 20)
AI_DEFAULT_DAILY_LIMIT = _env_int("AI_DEFAULT_DAILY_LIMIT", 10)
AI_CONTEXT_WINDOW = _env_int("AI_CONTEXT_WINDOW", 12)
AI_TIMEZONE = os.getenv("AI_TIMEZONE", "America/Cuiaba").strip()
AI_QUIET_HOURS_START = _env_optional_int("AI_QUIET_HOURS_START")
AI_QUIET_HOURS_END = _env_optional_int("AI_QUIET_HOURS_END")
AI_ENABLED = bool(AI_API_KEY and AI_API_URL and AI_MODEL)

