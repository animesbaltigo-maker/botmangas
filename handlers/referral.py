from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from config import BOT_USERNAME, WEBAPP_BASE_URL
from services.i18n import t_user

AFFILIATE_BANNER_URL = "https://photo.chelpbot.me/AgACAgEAAxkBaz9i2mnzeCnUmtUCTPw2T4wmM5Ko9-20AALSC2sb37mgR1jBwbjGWjhgAQADAgADeQADOwQ/photo.jpg"


def _referral_panel_text() -> str:
    return (
        "\U0001f4b8 <b>PROGRAMA DE AFILIADOS BALTIGO</b>\n\n"
        "<i>Transforme o bot em uma fonte de renda dentro do Telegram.</i>\n\n"
        "\U0001f680 <b>Como funciona</b>\n\n"
        "<blockquote>Voc\u00ea indica \u2192 a pessoa assina\n"
        "\u2192 voc\u00ea ganha comiss\u00e3o automaticamente\n\n"
        "Ap\u00f3s 7 dias, o valor fica dispon\u00edvel pra saque via Pix.</blockquote>\n\n"
        "\U0001f4b4 <b>Acesse seu painel para:</b>\n\n"
        "<blockquote>\u2022 \U0001f517 Ver e copiar seu link\n"
        "\u2022 \U0001f465 Acompanhar indicados\n"
        "\u2022 \U0001f4ca Ver cliques e convers\u00f5es\n"
        "\u2022 \U0001f4b0 Controlar comiss\u00f5es\n"
        "\u2022 \U0001f3e6 Solicitar saque</blockquote>\n\n"
        "<i>Toque no bot\u00e3o abaixo para abrir seu painel \U0001f447</i>"
    )


def _affiliate_webapp_url(user_id: int) -> str:
    base = (WEBAPP_BASE_URL or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/affiliate?user_id={int(user_id)}&bot={quote(BOT_USERNAME or '')}"


async def _send_panel(message, user_id: int):
    bot_username = BOT_USERNAME or ""
    link = f"https://t.me/{bot_username}?start=ref_{user_id}" if bot_username else ""
    app_url = _affiliate_webapp_url(user_id)

    text = _referral_panel_text()

    rows = []
    if app_url:
        rows.append([InlineKeyboardButton(t_user(user_id, "referral.open_panel"), web_app=WebAppInfo(url=app_url))])

    if link:
        telegram_share = (
            "https://t.me/share/url?"
            f"url={quote(link)}"
            "&text=" + quote(t_user(user_id, "referral.share_text"))
        )
        whatsapp_share = "https://wa.me/?text=" + quote(f"{t_user(user_id, 'referral.share_text')}\n{link}")
        rows.append([InlineKeyboardButton(t_user(user_id, "referral.share_telegram"), url=telegram_share)])
        rows.append([InlineKeyboardButton(t_user(user_id, "referral.share_whatsapp"), url=whatsapp_share)])

    if not app_url:
        text += "\n\n" + t_user(user_id, "referral.no_webapp")

    markup = InlineKeyboardMarkup(rows) if rows else None
    try:
        await message.reply_photo(
            photo=AFFILIATE_BANNER_URL,
            caption=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except Exception:
        await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )


async def indicacoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    await _send_panel(message, user.id)


async def referral_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    if query.data != "noop_indicar":
        return
    await query.answer()
    await _send_panel(query.message, user.id)
