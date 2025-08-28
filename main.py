# -*- coding: utf-8 -*-
"""
Cozy Asia Bot — webhook для Render + локальный polling.

ENV:
  TELEGRAM_BOT_TOKEN   — обяз.
  BASE_URL             — https://telegram-gpt-consultant-xxxx.onrender.com (пусто = polling)
  WEBHOOK_PATH         — /webhook
  PUBLIC_CHANNEL       — username канала без @
  GREETING_MESSAGE     — текст приветствия
  MANAGER_CHAT_ID      — чат ID менеджера (int, опц.)
  GOOGLE_SHEET_ID      — ID таблицы
  GOOGLE_CREDS_JSON    — JSON сервис-аккаунта целиком
  OPENAI_API_KEY       — ключ OpenAI (для свободного чата)
  LOG_LEVEL            — INFO/DEBUG...
"""

import os
import re
import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ConversationHandler,
    MessageHandler, ContextTypes, filters
)

# ================== ЛОГИ ==================
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cozy_bot")

# ================== ENV ===================
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
PUBLIC_CHANNEL = os.environ.get("PUBLIC_CHANNEL", "").lstrip("@").strip()
GREETING_MESSAGE = os.environ.get(
    "GREETING_MESSAGE",
    "Привет! Я ассистент Cozy Asia 🌴\nНапиши, что ищешь (район, бюджет, спальни, пожелания и т.д.) "
    "или нажми /rent — подберу варианты из базы.",
)
MANAGER_CHAT_ID = os.environ.get("MANAGER_CHAT_ID", "").strip() or None
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

# ================== GPT ===================
gpt_enabled = bool(OPENAI_API_KEY)
client = None
if gpt_enabled:
    try:
        from openai import OpenAI  # openai>=1.0
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        log.warning("OpenAI клиент не инициализирован: %s", e)
        gpt_enabled = False

async def gpt_reply(prompt: str, history: List[Dict[str, str]]) -> str:
    if not gpt_enabled or client is None:
        return "Свободный диалог доступен, но не задан ключ OPENAI_API_KEY."
    try:
        msgs = [{"role": "system", "content": "Ты дружелюбный риэлтор-ассистент на Самуи от Cozy Asia."}]
        msgs += history[-8:]
        msgs.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=msgs,
            temperature=0.4,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception("GPT error: %s", e)
        return "Пока не могу ответить (внутренняя ошибка GPT)."

# ============ Google Sheets ===============
gspread = None
sheet_ok = False
ws_leads = None
ws_listings = None

LEADS_SHEET_NAME = "Leads"
LEADS_COLUMNS = [
    "created_at", "chat_id", "username",
    "location", "bedrooms", "budget", "occupants",
    "pets", "pool", "workspace", "phone", "notes"
]

LISTINGS_SHEET_NAME = "Listings"
LISTING_COLUMNS = [
    "listing_id", "created_at", "title", "description", "location",
    "bedrooms", "bathrooms", "price_month", "pets_allowed", "utilities",
    "electricity_rate", "water_rate", "area_m2", "pool", "furnished",
    "link", "images", "tags", "raw_text"
]

def setup_gsheets():
    global gspread, sheet_ok, ws_leads, ws_listings
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDS_JSON:
        log.info("Sheets не настроен (нет GOOGLE_SHEET_ID/GOOGLE_CREDS_JSON).")
        return
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore

        creds = json.loads(GOOGLE_CREDS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        gc = gspread.authorize(Credentials.from_service_account_info(creds, scopes=scopes))
        sh = gc.open_by_key(GOOGLE_SHEET_ID)

        titles = [w.title for w in sh.worksheets()]
        if LEADS_SHEET_NAME in titles:
            ws_leads = sh.worksheet(LEADS_SHEET_NAME)
        else:
            ws_leads = sh.add_worksheet(LEADS_SHEET_NAME, rows="1000", cols=str(len(LEADS_COLUMNS)))
            ws_leads.append_row(LEADS_COLUMNS)

        if LISTINGS_SHEET_NAME in titles:
            ws_listings = sh.worksheet(LISTINGS_SHEET_NAME)
        else:
            ws_listings = sh.add_worksheet(LISTINGS_SHEET_NAME, rows="1000", cols=str(len(LISTING_COLUMNS)))
            ws_listings.append_row(LISTING_COLUMNS)

        sheet_ok = True
        log.info("Sheets подключен: Leads & Listings готовы.")
    except Exception as e:
        log.exception("Sheets init error: %s", e)
        sheet_ok = False

def leads_append(row: Dict[str, Any]):
    if sheet_ok and ws_leads:
        try:
            ws_leads.append_row([row.get(c, "") for c in LEADS_COLUMNS], value_input_option="USER_ENTERED")
        except Exception as e:
            log.exception("Sheets append lead error: %s", e)

def listings_append(row: Dict[str, Any]):
    if sheet_ok and ws_listings:
        try:
            ws_listings.append_row([row.get(c, "") for c in LISTING_COLUMNS], value_input_option="USER_ENTERED")
        except Exception as e:
            log.exception("Sheets append listing error: %s", e)

# ======= Парсер объявлений из канала ======
REGION_WORDS = [
    "lamai","lamaï","ламай","bophut","bo phut","бопхут","chaweng","чавенг",
    "maenam","маенам","bangrak","ban rak","банграк","банрак",
    "choeng mon","чоенг мон","липа ной","lipa noi","taling ngam","талинг"
]

def parse_listing_text(text: str, link: str, listing_id: str) -> Dict[str, Any]:
    t = text.lower()
    location = next((w for w in REGION_WORDS if w in t), "")
    m_bed = re.search(r"(\d+)\s*(спальн|bed)", t)
    m_bath = re.search(r"(\d+)\s*(ванн|bath)", t)
    m_price = re.search(r"(\d[\d\s]{3,})(?:\s*(?:baht|бат|฿|thb|b))?", t)
    return {
        "listing_id": listing_id,
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        "title": (text.strip().split("\n", 1)[0])[:80],
        "description": text[:2000],
        "location": location,
        "bedrooms": m_bed.group(1) if m_bed else "",
        "bathrooms": m_bath.group(1) if m_bath else "",
        "price_month": re.sub(r"\s", "", m_price.group(1)) if m_price else "",
        "pets_allowed": "unknown",
        "utilities": "unknown",
        "electricity_rate": "",
        "water_rate": "",
        "area_m2": "",
        "pool": "yes" if ("pool" in t or "бассейн" in t) else "no",
        "furnished": "unknown",
        "link": link,
        "images": "",
        "tags": "",
        "raw_text": text,
    }

# =============== Диалог /rent =============
(
    Q_LOCATION, Q_BEDROOMS, Q_BUDGET, Q_OCCUPANTS,
    Q_PETS, Q_POOL, Q_WORKSPACE, Q_PHONE, Q_NOTES
) = range(9)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(GREETING_MESSAGE)

async def cmd_rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🏝 В каком районе Самуи хотите жить? (Маенам, Бопхут, Чавенг, Ламай…)")
    return Q_LOCATION

async def ask_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["location"] = update.message.text.strip()
    await update.message.reply_text("🛏 Сколько спален нужно?")
    return Q_BEDROOMS

async def ask_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.search(r"\d+", update.message.text)
    context.user_data["bedrooms"] = int(m.group(0)) if m else 1
    await update.message.reply_text("💰 Какой бюджет в месяц (в батах)?")
    return Q_BUDGET

async def ask_occupants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.search(r"\d+", update.message.text.replace(" ", ""))
    context.user_data["budget"] = int(m.group(0)) if m else 0
    await update.message.reply_text("👥 Сколько человек будет проживать?")
    return Q_OCCUPANTS

async def ask_pets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.search(r"\d+", update.message.text)
    context.user_data["occupants"] = int(m.group(0)) if m else 1
    await update.message.reply_text("🐾 Есть ли питомцы? (да/нет)")
    return Q_PETS

async def ask_pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pets"] = ("да" in update.message.text.lower() or "yes" in update.message.text.lower())
    await update.message.reply_text("🏊 Нужен ли бассейн? (да/нет)")
    return Q_POOL

async def ask_workspace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pool"] = ("да" in update.message.text.lower() or "yes" in update.message.text.lower())
    await update.message.reply_text("💻 Важно ли рабочее место/кабинет? (да/нет)")
    return Q_WORKSPACE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["workspace"] = ("да" in update.message.text.lower() or "yes" in update.message.text.lower())
    await update.message.reply_text("📞 Укажите номер телефона для связи (или напишите «без телефона»).")
    return Q_PHONE

async def ask_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["phone"] = update.message.text.strip()
    await update.message.reply_text("📝 Дополнительные пожелания? (текстом)")
    return Q_NOTES

async def finish_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["notes"] = update.message.text.strip()
    user = update.effective_user
    lead = {
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        "chat_id": str(user.id),
        "username": user.username or f"{user.first_name or ''} {user.last_name or ''}".strip(),
        "location": context.user_data.get("location", ""),
        "bedrooms": context.user_data.get("bedrooms", ""),
        "budget": context.user_data.get("budget", ""),
        "occupants": context.user_data.get("occupants", ""),
        "pets": "да" if context.user_data.get("pets") else "нет",
        "pool": "да" if context.user_data.get("pool") else "нет",
        "workspace": "да" if context.user_data.get("workspace") else "нет",
        "phone": context.user_data.get("phone", ""),
        "notes": context.user_data.get("notes", ""),
    }
    leads_append(lead)

    if MANAGER_CHAT_ID:
        try:
            txt = (
                "Новая заявка 🧾\n"
                f"Район: {lead['location']}\n"
                f"Спальни: {lead['bedrooms']} | Бюджет: {lead['budget']}\n"
                f"Жильцов: {lead['occupants']}, Питомцы: {lead['pets']}\n"
                f"Бассейн: {lead['pool']}, Раб.место: {lead['workspace']}\n"
                f"Телефон: {lead['phone']}\n"
                f"Пожелания: {lead['notes']}\n"
                f"Пользователь: @{lead['username']} (id {lead['chat_id']})"
            )
            await context.bot.send_message(chat_id=int(MANAGER_CHAT_ID), text=txt)
        except Exception as e:
            log.warning("Не отправил менеджеру: %s", e)

    await update.message.reply_text("Спасибо! 🙌 Я передал заявку менеджеру. Мы скоро свяжемся.")
    return ConversationHandler.END

async def cancel_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Опрос отменён. Введите /rent чтобы начать заново.")
    return ConversationHandler.END

# ======= Свободный GPT-чат в ЛС ===========
async def chat_any_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.startswith("/"):
        return
    hist = context.chat_data.setdefault("history", [])
    hist.append({"role": "user", "content": text})
    hist[:] = hist[-10:]
    reply = await gpt_reply(text, hist)
    hist.append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)

# ======= Посты из канала ==================
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    row = parse_listing_text(text, link, f"{msg.chat.id}_{msg.message_id}")
    listings_append(row)
    log.info("Сохранён лот %s из @%s", row["listing_id"], uname)

# ============ Сборка приложения ===========
def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))

    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", cmd_rent)],
        states={
            Q_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_bedrooms)],
            Q_BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_budget)],
            Q_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_occupants)],
            Q_OCCUPANTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_pets)],
            Q_PETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_pool)],
            Q_POOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_workspace)],
            Q_WORKSPACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            Q_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_notes)],
            Q_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_form)],
        },
        fallbacks=[CommandHandler("cancel", cancel_form)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # GPT-чат в личке (последний, чтобы не перехватывал команды)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, chat_any_text))

    # Посты каналов
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, on_channel_post))

    return app

# ================== MAIN ==================
def main():
    setup_gsheets()
    app = build_app()

    me = app.bot.get_me()
    log.info("Bot: @%s", me.username)

    allowed = ["message", "channel_post", "callback_query"]

    if BASE_URL:
        webhook_url = f"{BASE_URL}{WEBHOOK_PATH if WEBHOOK_PATH.startswith('/') else '/'+WEBHOOK_PATH}"
        log.info("Starting webhook at %s", webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", "10000")),
            url_path=WEBHOOK_PATH.lstrip("/"),
            webhook_url=webhook_url,
            allowed_updates=allowed,
            drop_pending_updates=True,
        )
    else:
        log.info("Starting polling…")
        app.run_polling(allowed_updates=allowed, drop_pending_updates=True)

if __name__ == "__main__":
    main()
