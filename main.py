import os
import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackContext,
    filters,
)
from dateutil import parser as dtparser
import requests

# ==== ЛОГИ ====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("cozyasia-bot")


# ==== ENV ====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
PORT           = int(os.getenv("PORT", "10000"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("OPENAI_API_KEY_V1", "").strip()
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

GROUP_CHAT_ID  = os.getenv("GROUP_CHAT_ID", "").strip()  # например "-4908974521"
SHEET_ID       = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDS   = os.getenv("GOOGLE_CREDENTIALS", "").strip()  # JSON service account

# Ссылки на твои ресурсы
LINK_SITE      = os.getenv("LINK_SITE", "https://cozy.asia")
LINK_FEED      = os.getenv("LINK_FEED", "https://t.me/SamuiRental")
LINK_VILLAS    = os.getenv("LINK_VILLAS", "https://t.me/arenda_vill_samui")
LINK_IG        = os.getenv("LINK_IG", "https://www.instagram.com/cozy.asia")

# Быстрые кнопки под подсказкой
def promo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Открыть сайт", url=LINK_SITE)],
        [InlineKeyboardButton("📣 Телеграм-канал (все лоты)", url=LINK_FEED)],
        [InlineKeyboardButton("🏡 Канал по виллам", url=LINK_VILLAS)],
        [InlineKeyboardButton("📷 Instagram", url=LINK_IG)],
    ])


# ==== УТИЛИТЫ ====
def parse_date_human(s: str) -> Optional[str]:
    """
    Принимает даты в любом популярном виде: 01.12.2025, 2025-12-01, 1/12/25, 1 янв 2026 и т.п.
    Возвращает YYYY-MM-DD либо None.
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = dtparser.parse(s, dayfirst=True, fuzzy=True)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def safe_post_to_group(text: str, app: Application) -> None:
    if not GROUP_CHAT_ID:
        return
    try:
        app.bot.send_message(chat_id=int(GROUP_CHAT_ID), text=text, disable_web_page_preview=True)
    except Exception as e:
        log.warning("Cannot post to group: %s", e)


def openai_chat(messages: list[Dict[str, str]]) -> str:
    """
    Простой вызов OpenAI Responses API без сторонних SDK (чтобы не ловить несовместимости).
    """
    if not OPENAI_API_KEY:
        return "Сейчас не могу достучаться до модели ИИ. Напишите /rent, а также смотрите ссылки ниже."
    try:
        url = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": OPENAI_MODEL,
            "messages": messages,
            "temperature": 0.7,
        }
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        res = requests.post(url, headers=headers, json=payload, timeout=30)
        res.raise_for_status()
        data = res.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("OpenAI error: %s", e)
        return "Похоже, ИИ сейчас недоступен. Я всё равно могу помочь: нажмите /rent или откройте ссылки ниже."


# ==== GOOGLE SHEETS ====
def sheets_append(row: Dict[str, Any]) -> None:
    """
    Пишем строку лида в Google Sheets (в шит 'Leads').
    Никак не валим бот, если что-то не настроено — просто лог и всё.
    """
    if not (SHEET_ID and GOOGLE_CREDS):
        log.info("Sheets disabled: no SHEET_ID or GOOGLE_CREDENTIALS")
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_dict = json.loads(GOOGLE_CREDS)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SHEET_ID)
        ws = sh.worksheet("Leads")

        values = [
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            str(row.get("chat_id", "")),
            str(row.get("username", "")),
            str(row.get("type", "")),
            str(row.get("area", "")),
            str(row.get("bedrooms", "")),
            str(row.get("budget", "")),
            str(row.get("checkin", "")),
            str(row.get("checkout", "")),
            str(row.get("notes", "")),
        ]
        ws.append_row(values, value_input_option="RAW")
        log.info("Lead appended to sheet")
    except Exception as e:
        log.warning("Sheets append failed: %s", e)


# ==== СОСТОЯНИЯ ОПРОСНИКА ====
(
    Q_TYPE,
    Q_BUDGET,
    Q_AREA,
    Q_BEDS,
    Q_CHECKIN,
    Q_CHECKOUT,
    Q_NOTES,
) = range(7)

def start_text() -> str:
    return (
        "🖐️ Привет! Добро пожаловать в «Cozy Asia Real Estate Bot»\n\n"
        "😊 Я твой ИИ помощник и консультант. Со мной можно говорить так же свободно, как с человеком.\n\n"
        "❓ Задавай вопросы:\n"
        "🏡 про дома, виллы и квартиры на Самуи\n"
        "🌴 про жизнь на острове, районы, атмосферу и погоду\n"
        "🍹 про быт, отдых и куда сходить на острове\n\n"
        "🔧 Самый действенный способ — пройти короткую анкету командой /rent.\n"
        "Я сделаю подборку лотов по вашим критериям и передам менеджеру."
    )

async def cmd_start(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text(start_text(), reply_markup=promo_keyboard())

async def cmd_cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Окей, останавливаю анкету. Можно спросить меня что угодно.")
    return ConversationHandler.END


# ==== ОПРОС /rent ====
async def rent_entry(update: Update, context: CallbackContext) -> int:
    context.user_data["lead"] = {
        "chat_id": update.effective_user.id,
        "username": update.effective_user.username or update.effective_user.full_name,
    }
    await update.message.reply_text("Начнём подбор.\n1/7. Какой тип жилья интересует: квартира, дом или вилла?")
    return Q_TYPE

async def rent_type(update: Update, context: CallbackContext) -> int:
    context.user_data["lead"]["type"] = update.message.text.strip()
    await update.message.reply_text("2/7. Какой у вас бюджет в батах (месяц)?")
    return Q_BUDGET

async def rent_budget(update: Update, context: CallbackContext) -> int:
    context.user_data["lead"]["budget"] = update.message.text.strip()
    await update.message.reply_text("3/7. В каком районе Самуи предпочтительно жить?")
    return Q_AREA

async def rent_area(update: Update, context: CallbackContext) -> int:
    context.user_data["lead"]["area"] = update.message.text.strip()
    await update.message.reply_text("4/7. Сколько нужно спален?")
    return Q_BEDS

async def rent_beds(update: Update, context: CallbackContext) -> int:
    context.user_data["lead"]["bedrooms"] = update.message.text.strip()
    await update.message.reply_text("5/7. Дата заезда (в любом формате, например 01.12.2025)?")
    return Q_CHECKIN

async def rent_checkin(update: Update, context: CallbackContext) -> int:
    dt = parse_date_human(update.message.text)
    if not dt:
        await update.message.reply_text("Не распознал дату. Напишите ещё раз (например 2025-12-01).")
        return Q_CHECKIN
    context.user_data["lead"]["checkin"] = dt
    await update.message.reply_text("6/7. Дата выезда (в любом формате, например 01.01.2026)?")
    return Q_CHECKOUT

async def rent_checkout(update: Update, context: CallbackContext) -> int:
    dt = parse_date_human(update.message.text)
    if not dt:
        await update.message.reply_text("Не распознал дату. Напишите ещё раз (например 2026-01-01).")
        return Q_CHECKOUT
    context.user_data["lead"]["checkout"] = dt
    await update.message.reply_text("7/7. Важные условия? (близость к пляжу, с питомцами, парковка и т.п.)")
    return Q_NOTES

def format_lead_card(lead: Dict[str, Any], user_mention: str) -> str:
    return (
        "🆕 Новая заявка Cozy Asia\n"
        f"Клиент: {user_mention}\n"
        f"Тип: {lead.get('type','')}\n"
        f"Район: {lead.get('area','')}\n"
        f"Бюджет: {lead.get('budget','')}\n"
        f"Спален: {lead.get('bedrooms','')}\n"
        f"Check-in: {lead.get('checkin','')} | Check-out: {lead.get('checkout','')}\n"
        f"Условия/прим.: {lead.get('notes','')}\n"
        f"Создано: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )

async def rent_notes(update: Update, context: CallbackContext) -> int:
    lead = context.user_data.get("lead", {})
    lead["notes"] = update.message.text.strip()

    # 1) Пишем в таблицу (без падений)
    sheets_append({
        "chat_id": lead.get("chat_id"),
        "username": lead.get("username"),
        "type": lead.get("type"),
        "area": lead.get("area"),
        "bedrooms": lead.get("bedrooms"),
        "budget": lead.get("budget"),
        "checkin": lead.get("checkin"),
        "checkout": lead.get("checkout"),
        "notes": lead.get("notes"),
    })

    # 2) Уведомляем рабочую группу
    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.full_name
    safe_post_to_group(format_lead_card(lead, user_mention), context.application)

    # 3) Отвечаем пользователю + мягкий переход к свободному чату
    txt = (
        "Готово! Заявка сформирована и передана менеджеру ✅\n"
        "Я также подберу варианты по вашим критериям и пришлю вам. "
        "Пока можно продолжать свободный разговор — я на связи.\n\n"
        "Если хотите, нажмите /rent, чтобы отправить ещё одну заявку."
    )
    await update.message.reply_text(txt, reply_markup=promo_keyboard())
    return ConversationHandler.END


# ==== СВОБОДНЫЙ GPT-ЧАТ ====
SYSTEM_PROMPT = (
    "Ты — дружелюбный ассистент Cozy Asia для острова Самуи. Отвечай по делу, кратко и полезно. "
    "Когда вопрос касается аренды/покупки/продажи или «где посмотреть варианты», не отправляй к сторонним агентствам — "
    "всегда мягко направляй к нашим ресурсам и предлагай анкету /rent. "
    "Но при этом отвечай на любые обычные вопросы (погода, пляжи, районы, быт и т.д.)."
)

def build_messages(user_text: str, username: str) -> list[Dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Пользователь (@{username}): {user_text}"},
    ]

async def free_chat(update: Update, context: CallbackContext) -> None:
    text = update.message.text or ""
    username = update.effective_user.username or update.effective_user.full_name
    log.info("TEXT from %s: %s", username, text)

    answer = openai_chat(build_messages(text, username))
    tail = (
        "\n\n🔧 Самый действенный способ — пройти короткую анкету /rent. "
        "Сделаю подборку лотов по вашим критериям и передам менеджеру.\n\n"
        f"• Сайт: {LINK_SITE}\n"
        f"• Канал с лотами: {LINK_FEED}\n"
        f"• Канал по виллам: {LINK_VILLAS}\n"
        f"• Instagram: {LINK_IG}"
    )
    try:
        await update.message.reply_text(answer + tail, reply_markup=promo_keyboard(), disable_web_page_preview=True)
    except Exception as e:
        log.error("Reply error: %s", e)


# ==== ОШИБКИ ====
async def on_error(update: Optional[Update], context: CallbackContext) -> None:
    log.exception("Exception while handling update: %s", context.error)


# ==== ПРИЛОЖЕНИЕ ====
def build_application() -> Application:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("ENV TELEGRAM_TOKEN is required")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Опросник /rent
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_entry)],
        states={
            Q_TYPE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_type)],
            Q_BUDGET:   [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_budget)],
            Q_AREA:     [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_area)],
            Q_BEDS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_beds)],
            Q_CHECKIN:  [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_checkin)],
            Q_CHECKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_checkout)],
            Q_NOTES:    [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_notes)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        per_chat=True,
        per_user=True,
    )
    app.add_handler(conv)

    # Свободный чат — в самом конце, чтобы ловить всё остальное
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_chat))

    # Ошибки
    app.add_error_handler(on_error)
    return app


def main() -> None:
    app = build_application()

    # Локальный запуск (без вебхука) — на всякий случай
    if not WEBHOOK_BASE:
        log.info("Starting long-polling (WEBHOOK_BASE not set)")
        app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)
        return

    # Render: вебхук
    path = f"/webhook/{TELEGRAM_TOKEN}"
    url = f"{WEBHOOK_BASE}{path}"
    log.info("=> run_webhook port=%s url=%s", PORT, url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=f"{TELEGRAM_TOKEN}",
        webhook_url=url,
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
