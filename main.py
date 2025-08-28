# -*- coding: utf-8 -*-
"""
Cozy Asia Bot — единый main.py
- Webhook для Render и polling локально (выбор по BASE_URL).
- Снимает старые вебхуки с drop_pending_updates=True (нет 409).
- Анкета /rent со всеми шагами + запись лида в Google Sheets (лист 'Leads').
- Свободный GPT-чат для любых сообщений вне анкеты (если задан OPENAI_API_KEY).
- Парсинг постов канала и запись лотов на лист 'Listings'.
"""

import os
import re
import json
import logging
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests  # для GPT (без отдельной библиотеки)
from telegram import Update, Bot
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

# -------------------- ЛОГИ --------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cozy_bot")

# -------------------- ENV --------------------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]  # обязательна
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
PUBLIC_CHANNEL = os.environ.get("PUBLIC_CHANNEL", "").lstrip("@").strip()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()

GREETING_MESSAGE = os.environ.get(
    "GREETING_MESSAGE",
    "Привет! Я ассистент Cozy Asia 🌴\n"
    "Напиши, что ищешь (район, бюджет, спальни, пожелания и т.д.) или нажми /rent — "
    "я помогу подобрать варианты."
)

MANAGER_CHAT_ID = os.environ.get("MANAGER_CHAT_ID", "").strip() or None

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "").strip()

# ---------------- Google Sheets ----------------
gspread = None
LeadsWS = None
ListingsWS = None
sheets_ready = False

LEADS_COLUMNS = [
    "created_at", "chat_id", "username", "location", "budget",
    "bedrooms", "preferences", "pool", "workspace", "people", "phone"
]

LISTINGS_COLUMNS = [
    "listing_id", "created_at", "title", "description", "location", "bedrooms",
    "bathrooms", "price_month", "pets_allowed", "utilities", "electricity_rate",
    "water_rate", "area_m2", "pool", "furnished", "link", "images", "tags", "raw_text"
]

def setup_sheets() -> None:
    global gspread, LeadsWS, ListingsWS, sheets_ready
    if not (GOOGLE_SHEET_ID and GOOGLE_CREDS_JSON):
        log.info("Google Sheets: переменные не заданы — запись отключена.")
        sheets_ready = False
        return
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDS_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        sh = client.open_by_key(GOOGLE_SHEET_ID)

        # Leads
        if "Leads" in [w.title for w in sh.worksheets()]:
            LeadsWS = sh.worksheet("Leads")
        else:
            LeadsWS = sh.add_worksheet(title="Leads", rows="1000", cols=str(len(LEADS_COLUMNS)))
            LeadsWS.append_row(LEADS_COLUMNS)

        # Listings
        if "Listings" in [w.title for w in sh.worksheets()]:
            ListingsWS = sh.worksheet("Listings")
        else:
            ListingsWS = sh.add_worksheet(title="Listings", rows="1000", cols=str(len(LISTINGS_COLUMNS)))
            ListingsWS.append_row(LISTINGS_COLUMNS)

        sheets_ready = True
        log.info("Google Sheets подключен: Leads & Listings готовы.")
    except Exception as e:
        log.exception("Google Sheets init error: %s", e)
        sheets_ready = False


def append_lead_row(data: Dict[str, Any]) -> None:
    if not (sheets_ready and LeadsWS):
        return
    try:
        row = [data.get(col, "") for col in LEADS_COLUMNS]
        LeadsWS.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.exception("Ошибка записи лида: %s", e)


def append_listing_row(data: Dict[str, Any]) -> None:
    if not (sheets_ready and ListingsWS):
        return
    try:
        row = [data.get(col, "") for col in LISTINGS_COLUMNS]
        ListingsWS.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.exception("Ошибка записи листинга: %s", e)

# ---------------- Парсер постов канала ----------------
REGION_WORDS = [
    "lamai", "lamaï", "lamay", "ламай",
    "bophut", "bo phut", "бопхут",
    "chaweng", "чавенг",
    "maenam", "маенам",
    "ban rak", "bangrak", "bang rak", "банрак", "банграк",
    "choeng mon", "чоенг мон", "чоэнг мон",
    "lipanoi", "lipa noi", "липа ной",
    "taling ngam", "талинг ньгам", "талиннгам"
]

def parse_listing(text: str, link: str, listing_id: str) -> Dict[str, Any]:
    t = text.lower()

    def find_num(pattern: str) -> str:
        m = re.search(pattern, t)
        return m.group(1) if m else ""

    location = next((w for w in REGION_WORDS if w in t), "")
    bedrooms = find_num(r"(\d+)\s*(спальн|bed(room)?s?)")
    bathrooms = find_num(r"(\d+)\s*(ванн|bath(room)?s?)")

    price = ""
    mp = re.search(r"(\d[\d\s]{3,})(?:\s*(?:baht|бат|฿|b|thb))?", t)
    if mp:
        price = re.sub(r"\s", "", mp.group(1))

    pets_allowed = "unknown"
    if "без питомц" in t or "no pets" in t:
        pets_allowed = "no"
    elif "с питомц" in t or "pets ok" in t or "pet friendly" in t:
        pets_allowed = "yes"

    pool = "yes" if ("pool" in t or "бассейн" in t) else "no"
    furnished = "yes" if ("furnished" in t or "мебел" in t) else "unknown"

    title = ""
    mt = re.search(r"^([^\n]{10,80})", text.strip())
    if mt:
        title = mt.group(1)

    return {
        "listing_id": listing_id,
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        "title": title,
        "description": text[:2000],
        "location": location,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "price_month": price,
        "pets_allowed": pets_allowed,
        "utilities": "unknown",
        "electricity_rate": "",
        "water_rate": "",
        "area_m2": "",
        "pool": pool,
        "furnished": furnished,
        "link": link,
        "images": "",
        "tags": "",
        "raw_text": text,
    }

# ---------------- GPT (свободный чат) ----------------
def gpt_reply(prompt: str, username: str = "") -> str:
    """Лёгкий HTTP-клиент к OpenAI Chat Completions. Возвращает текст или заготовленный ответ."""
    if not OPENAI_API_KEY:
        # fallback — если ключа нет, вежливо ответим
        return "Я тут! Чтобы поболтать в свободной форме, добавь OPENAI_API_KEY в переменные окружения. А пока могу помочь с арендой — набери /rent 😊"

    try:
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": OPENAI_MODEL or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "Ты дружелюбный ассистент агентства аренды недвижимости на Самуи. Отвечай кратко и по делу. Если уместно — предлагай команду /rent для подбора жилья."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.6,
        }
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          headers=headers, data=json.dumps(payload), timeout=30)
        r.raise_for_status()
        data = r.json()
        txt = data["choices"][0]["message"]["content"].strip()
        return txt or "Готов помочь!"
    except Exception as e:
        log.warning("OpenAI error: %s", e)
        return "Похоже, сейчас недоступен GPT. Можем пройти анкету — /rent"

# ---------------- Анкета /rent ----------------
(ASK_LOCATION, ASK_BUDGET, ASK_BEDROOMS,
 ASK_PREFERENCES, ASK_POOL, ASK_WORKSPACE,
 ASK_PEOPLE, ASK_PHONE, CONFIRM) = range(9)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(GREETING_MESSAGE)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Команды:\n/start — приветствие\n/rent — подбор жилья по анкете\n/cancel — отменить анкету")

async def rent_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("🏝 В каком районе Самуи хотите жить? (Маенам, Бопхут, Чавенг, Ламай…)")
    return ASK_LOCATION

async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["location"] = update.message.text.strip()
    await update.message.reply_text("💸 Какой бюджет в месяц (бат)?")
    return ASK_BUDGET

async def on_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = update.message.text.replace(" ", "")
    m = re.search(r"\d+", txt)
    context.user_data["budget"] = int(m.group(0)) if m else 0
    await update.message.reply_text("🛏 Сколько спален нужно?")
    return ASK_BEDROOMS

async def on_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = re.search(r"\d+", update.message.text)
    context.user_data["bedrooms"] = int(m.group(0)) if m else 1
    await update.message.reply_text("✨ Есть особые пожелания? (например: вид на море, закрытая территория, с питомцами)")
    return ASK_PREFERENCES

async def on_preferences(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["preferences"] = update.message.text.strip()
    await update.message.reply_text("🏊 Нужен бассейн? (да/нет/не важно)")
    return ASK_POOL

async def on_pool(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["pool"] = update.message.text.strip()
    await update.message.reply_text("💻 Нужна рабочая зона/стол? (да/нет/не важно)")
    return ASK_WORKSPACE

async def on_workspace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["workspace"] = update.message.text.strip()
    await update.message.reply_text("👥 Сколько человек будет проживать?")
    return ASK_PEOPLE

async def on_people(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = re.search(r"\d+", update.message.text)
    context.user_data["people"] = int(m.group(0)) if m else 1
    await update.message.reply_text("📞 Оставьте телефон для связи (или напишите 'нет'):")
    return ASK_PHONE

async def on_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["phone"] = update.message.text.strip()

    # Финальный резюме
    d = context.user_data
    resume = (
        "✅ Резюме заявки:\n"
        f"• Район: {d.get('location')}\n"
        f"• Бюджет: {d.get('budget')} бат/мес\n"
        f"• Спален: {d.get('bedrooms')}\n"
        f"• Пожелания: {d.get('preferences')}\n"
        f"• Бассейн: {d.get('pool')}\n"
        f"• Рабочее место: {d.get('workspace')}\n"
        f"• Проживать: {d.get('people')}\n"
        f"• Телефон: {d.get('phone')}\n\n"
        "Если всё верно, отправляю менеджеру. Спасибо! 🙌"
    )
    await update.message.reply_text(resume)

    # Запись лида
    lead = {
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        "chat_id": update.effective_user.id if update.effective_user else "",
        "username": (update.effective_user.username if update.effective_user and update.effective_user.username else
                     f"{update.effective_user.first_name if update.effective_user else ''} {update.effective_user.last_name or ''}".strip()),
        "location": d.get("location", ""),
        "budget": d.get("budget", ""),
        "bedrooms": d.get("bedrooms", ""),
        "preferences": d.get("preferences", ""),
        "pool": d.get("pool", ""),
        "workspace": d.get("workspace", ""),
        "people": d.get("people", ""),
        "phone": d.get("phone", ""),
    }
    append_lead_row(lead)

    # Уведомление менеджеру
    if MANAGER_CHAT_ID:
        try:
            text = "Новая заявка:\n" + "\n".join([f"{k}: {v}" for k, v in lead.items()])
            await context.bot.send_message(chat_id=int(MANAGER_CHAT_ID), text=text)
        except Exception as e:
            log.warning("Не удалось отправить менеджеру: %s", e)

    await update.message.reply_text("Спасибо! ✋ Я передал заявку менеджеру. Мы скоро свяжемся.")
    return ConversationHandler.END

async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Опрос отменён. Введите /rent чтобы начать заново.")
    return ConversationHandler.END

# ---------------- Канальные посты → Listings ----------------
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.channel_post:
        return
    msg = update.channel_post
    uname = (msg.chat.username or "").lower()
    if PUBLIC_CHANNEL and uname != PUBLIC_CHANNEL.lower():
        return

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    link = msg.link or (f"https://t.me/{msg.chat.username}/{msg.message_id}" if msg.chat.username else "")
    listing_id = f"{msg.chat.id}_{msg.message_id}"
    item = parse_listing(text, link, listing_id)
    append_listing_row(item)
    log.info("Сохранён лот %s из канала @%s", listing_id, msg.chat.username)

# ---------------- Свободный GPT-чат ----------------
async def free_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.message.text.strip()
    # не перехватываем команды
    if user_text.startswith("/"):
        return
    reply = gpt_reply(user_text, username=update.effective_user.username if update.effective_user else "")
    await update.message.reply_text(reply)

# ---------------- Сборка приложения ----------------
def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    # Анкета /rent
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_start)],
        states={
            ASK_LOCATION:  [MessageHandler(filters.TEXT & ~filters.COMMAND, on_location)],
            ASK_BUDGET:    [MessageHandler(filters.TEXT & ~filters.COMMAND, on_budget)],
            ASK_BEDROOMS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, on_bedrooms)],
            ASK_PREFERENCES:[MessageHandler(filters.TEXT & ~filters.COMMAND, on_preferences)],
            ASK_POOL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, on_pool)],
            ASK_WORKSPACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_workspace)],
            ASK_PEOPLE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, on_people)],
            ASK_PHONE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, on_phone)],
        },
        fallbacks=[CommandHandler("cancel", rent_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Канальный постинг
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & (filters.TEXT | filters.CAPTION), on_channel_post))

    # Свободный чат — в самом конце, чтобы не мешать анкете/командам
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_chat))

    return app

# ---------------- Запуск ----------------
async def run_webhook(app: Application) -> None:
    """Webhook режим для Render."""
    bot: Bot = app.bot
    await bot.delete_webhook(drop_pending_updates=True)
    webhook_url = f"{BASE_URL}{WEBHOOK_PATH if WEBHOOK_PATH.startswith('/') else '/'+WEBHOOK_PATH}"
    await bot.set_webhook(url=webhook_url, allowed_updates=["message", "channel_post", "callback_query"])
    log.info("Starting webhook at %s (PORT=%s)", webhook_url, os.environ.get("PORT", "10000"))

    # ВАЖНО: не создаём/не закрываем свой цикл — даём PTB управлять им.
    await app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        url_path=WEBHOOK_PATH.lstrip("/"),
        webhook_url=webhook_url,
        allowed_updates=["message", "channel_post", "callback_query"],
    )

async def run_polling(app: Application) -> None:
    """Локальный режим polling."""
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.run_polling(allowed_updates=["message", "channel_post", "callback_query"])

def main() -> None:
    setup_sheets()
    app = build_app()

    async def _runner():
        me = await app.bot.get_me()
        log.info("Bot started: @%s", me.username)
        if BASE_URL:
            await run_webhook(app)
        else:
            await run_polling(app)

    # Запускаем безопасно единым asyncio.run (исключает 'Cannot close a running event loop')
    asyncio.run(_runner())

if __name__ == "__main__":
    main()
