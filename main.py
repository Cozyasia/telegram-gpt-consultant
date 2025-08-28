# -*- coding: utf-8 -*-
"""
Cozy Asia Bot — единый main.py

Функции:
- Webhook (Render) и Polling (локально).
- Удаление старого вебхука с drop_pending_updates=True (нет 409).
- Анкета /rent (расширенная) -> запись в Google Sheets (лист "Leads").
- GPT-ответы на любые свободные сообщения вне анкеты.
- Парсинг постов канала -> лист "Listings" (как раньше).

ENV (точные имена):
  TELEGRAM_BOT_TOKEN   (обяз.)
  BASE_URL             (например https://telegram-gpt-consultant-xxxx.onrender.com)
  WEBHOOK_PATH         (например /webhook — оставить так)
  PUBLIC_CHANNEL       (юзернейм канала без @, например samuirental)
  GREETING_MESSAGE     (кастомный /start)
  MANAGER_CHAT_ID      (чат ID менеджера, опционально)
  GOOGLE_SHEET_ID      (ID таблицы)
  GOOGLE_CREDS_JSON    (JSON сервис-аккаунта целиком)
  OPENAI_API_KEY       (для GPT-ответов; если нет — бот просто молчит вне анкеты)
  LOG_LEVEL            (INFO/DEBUG и т.п.)
"""

import os
import re
import json
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from telegram import Update, Bot, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, ConversationHandler, MessageHandler,
    ContextTypes, filters
)

# ---------- ЛОГИ ----------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cozy_bot")

# ---------- ENV ----------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
PUBLIC_CHANNEL = os.environ.get("PUBLIC_CHANNEL", "").lstrip("@").strip()

GREETING_MESSAGE = os.environ.get(
    "GREETING_MESSAGE",
    "Привет! Я ассистент Cozy Asia 🌴\n"
    "Напиши, что ищешь (район, бюджет, спальни, пожелания и т.д.) или нажми /rent — я помогу подобрать варианты.",
)
MANAGER_CHAT_ID = os.environ.get("MANAGER_CHAT_ID", "").strip() or None

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "").strip()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# ---------- Google Sheets ----------
gspread = None
leads_ws = None
listings_ws = None

LEADS_SHEET_NAME = "Leads"
LEADS_COLUMNS = [
    "created_at", "chat_id", "username", "full_name", "phone",
    "location", "bedrooms", "budget",
    "pets", "pool", "workspace", "people",
    "notes"
]

LISTINGS_SHEET_NAME = "Listings"
LISTING_COLUMNS = [
    "listing_id", "created_at", "title", "description", "location", "bedrooms",
    "bathrooms", "price_month", "pets_allowed", "utilities", "electricity_rate",
    "water_rate", "area_m2", "pool", "furnished", "link", "images", "tags", "raw_text"
]

def setup_gsheets() -> None:
    global gspread, leads_ws, listings_ws
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDS_JSON:
        log.info("Google Sheets не настроен (нет GOOGLE_SHEET_ID/GOOGLE_CREDS_JSON).")
        return
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sh = client.open_by_key(GOOGLE_SHEET_ID)

        # Leads
        if LEADS_SHEET_NAME in [w.title for w in sh.worksheets()]:
            leads_ws = sh.worksheet(LEADS_SHEET_NAME)
        else:
            leads_ws = sh.add_worksheet(title=LEADS_SHEET_NAME, rows="1000", cols=str(len(LEADS_COLUMNS)))
            leads_ws.append_row(LEADS_COLUMNS)

        # Listings
        if LISTINGS_SHEET_NAME in [w.title for w in sh.worksheets()]:
            listings_ws = sh.worksheet(LISTINGS_SHEET_NAME)
        else:
            listings_ws = sh.add_worksheet(title=LISTINGS_SHEET_NAME, rows="1000", cols=str(len(LISTING_COLUMNS)))
            listings_ws.append_row(LISTING_COLUMNS)

        log.info("Google Sheets подключен (листы: %s, %s)", LEADS_SHEET_NAME, LISTINGS_SHEET_NAME)
    except Exception as e:
        log.exception("Ошибка подключения к Google Sheets: %s", e)

def leads_append(row: Dict[str, Any]) -> None:
    if not leads_ws:
        return
    try:
        values = [row.get(c, "") for c in LEADS_COLUMNS]
        leads_ws.append_row(values, value_input_option="USER_ENTERED")
    except Exception as e:
        log.exception("Ошибка записи в Leads: %s", e)

def listings_append(row: Dict[str, Any]) -> None:
    if not listings_ws:
        return
    try:
        values = [row.get(c, "") for c in LISTING_COLUMNS]
        listings_ws.append_row(values, value_input_option="USER_ENTERED")
    except Exception as e:
        log.exception("Ошибка записи в Listings: %s", e)

# ---------- Простой GPT-ответ ----------
def gpt_reply(text: str, user_name: str = "") -> Optional[str]:
    if not OPENAI_API_KEY:
        return None
    try:
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        data = {
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system",
                 "content": "Ты дружелюбный русскоязычный ассистент агентства аренды на Самуи. "
                            "Отвечай кратко, по делу. Если вопрос про аренду — помогай и предлагай написать /rent для подбора."},
                {"role": "user", "content": text}
            ],
            "temperature": 0.3
        }
        resp = requests.post("https://api.openai.com/v1/chat/completions",
                             headers=headers, json=data, timeout=20)
        resp.raise_for_status()
        j = resp.json()
        return j["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning("GPT error: %s", e)
        return None

# ---------- Парсер постов канала (как было) ----------
REGION_WORDS = [
    "lamai","lamaï","lamay","ламай","bophut","bo phut","бопхут","chaweng","чавенг",
    "maenam","маенам","ban rak","bangrak","bang rak","банрак","банграк",
    "choeng mon","чоенг мон","чоэнг мон","lipanoi","lipa noi","липа ной",
    "taling ngam","талинг ньгам","талиннгам"
]

def parse_listing(text: str, msg_link: str, listing_id: str) -> Dict[str, Any]:
    t = text.lower()
    location = ""
    for w in REGION_WORDS:
        if w in t:
            location = w
            break
    def pick(regex: str) -> str:
        m = re.search(regex, t)
        return m.group(1) if m else ""
    bedrooms = pick(r"(\d+)\s*(спальн|bed(room)?s?)")
    bathrooms = pick(r"(\d+)\s*(ванн|bath(room)?s?)")
    price = ""
    mp = re.search(r"(\d[\d\s]{3,})(?:\s*baht|\s*бат|\s*฿|b|thb)?", t)
    if mp: price = re.sub(r"\s","",mp.group(1))

    pets = "unknown"
    if "без питомц" in t or "no pets" in t: pets = "no"
    elif "с питомц" in t or "pets ok" in t or "pet friendly" in t: pets = "yes"

    pool = "yes" if ("pool" in t or "бассейн" in t) else "no"
    furnished = "yes" if ("furnished" in t or "мебел" in t) else "unknown"
    title = (re.search(r"^([^\n]{10,80})", text.strip()) or ["",""])[1] if re.search(r"^([^\n]{10,80})", text.strip()) else ""

    return {
        "listing_id": listing_id,
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        "title": title,
        "description": text[:2000],
        "location": location,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "price_month": price,
        "pets_allowed": pets,
        "utilities": "unknown",
        "electricity_rate": "",
        "water_rate": "",
        "area_m2": "",
        "pool": pool,
        "furnished": furnished,
        "link": msg_link,
        "images": "",
        "tags": "",
        "raw_text": text,
    }

# ---------- Анкета /rent ----------
(
    AREA, BEDROOMS, BUDGET, PETS, POOL, WORKSPACE, PEOPLE, PHONE, NOTES
) = range(9)

def kb_remove():
    return ReplyKeyboardRemove()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(GREETING_MESSAGE)

async def cmd_rent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🏝 В каком районе Самуи хотите жить? (Маенам, Бопхут, Ламай, Чавенг…)",
        reply_markup=kb_remove(),
    )
    context.user_data.clear()
    return AREA

async def st_area(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["location"] = update.message.text.strip()
    await update.message.reply_text("🛏 Сколько спален нужно?")
    return BEDROOMS

async def st_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = re.search(r"\d+", update.message.text or "")
    context.user_data["bedrooms"] = int(m.group(0)) if m else 1
    await update.message.reply_text("💰 Какой бюджет в месяц (в бат)?")
    return BUDGET

async def st_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = re.search(r"\d+", update.message.text.replace(" ", "") if update.message.text else "")
    context.user_data["budget"] = int(m.group(0)) if m else 0
    await update.message.reply_text("🐶 Питомцы? (да/нет/неважно)")
    return PETS

async def st_pets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = (update.message.text or "").lower()
    if "да" in txt or "есть" in txt: context.user_data["pets"] = "yes"
    elif "нет" in txt: context.user_data["pets"] = "no"
    else: context.user_data["pets"] = "any"
    await update.message.reply_text("🏊 Нужен бассейн? (да/нет/неважно)")
    return POOL

async def st_pool(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = (update.message.text or "").lower()
    context.user_data["pool"] = "yes" if "да" in txt else ("no" if "нет" in txt else "any")
    await update.message.reply_text("💻 Нужна рабочая зона/стол? (да/нет/неважно)")
    return WORKSPACE

async def st_workspace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = (update.message.text or "").lower()
    context.user_data["workspace"] = "yes" if "да" in txt else ("no" if "нет" in txt else "any")
    await update.message.reply_text("👨‍👩‍👧‍👦 Сколько человек будет проживать?")
    return PEOPLE

async def st_people(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = re.search(r"\d+", update.message.text or "")
    context.user_data["people"] = int(m.group(0)) if m else 1
    kb = ReplyKeyboardMarkup([[KeyboardButton("Отправить номер", request_contact=True)]], resize_keyboard=True)
    await update.message.reply_text("📞 Оставьте номер телефона (впишите текстом или нажмите кнопку):", reply_markup=kb)
    return PHONE

async def st_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = ""
    if update.message.contact and update.message.contact.phone_number:
        phone = update.message.contact.phone_number
    else:
        phone = (update.message.text or "").strip()
    context.user_data["phone"] = phone
    await update.message.reply_text("📝 Есть ли дополнительные пожелания? Напишите одним сообщением.", reply_markup=kb_remove())
    return NOTES

async def st_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["notes"] = (update.message.text or "").strip()

    u = update.effective_user
    lead = {
        "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "chat_id": str(update.effective_chat.id),
        "username": (u.username or ""),
        "full_name": f"{u.first_name or ''} {u.last_name or ''}".strip(),
        "phone": context.user_data.get("phone",""),
        "location": context.user_data.get("location",""),
        "bedrooms": str(context.user_data.get("bedrooms","")),
        "budget": str(context.user_data.get("budget","")),
        "pets": context.user_data.get("pets",""),
        "pool": context.user_data.get("pool",""),
        "workspace": context.user_data.get("workspace",""),
        "people": str(context.user_data.get("people","")),
        "notes": context.user_data.get("notes",""),
    }
    leads_append(lead)
    log.info("Lead saved: %s", lead)

    # уведомим менеджера
    if MANAGER_CHAT_ID:
        try:
            txt = (
                "Новая заявка:\n"
                f"• Пользователь: @{lead['username'] or '—'} ({lead['full_name']})\n"
                f"• Телефон: {lead['phone']}\n"
                f"• Район: {lead['location']}\n"
                f"• Спален: {lead['bedrooms']}\n"
                f"• Бюджет: {lead['budget']}\n"
                f"• Питомцы: {lead['pets']}, Бассейн: {lead['pool']}, Workspace: {lead['workspace']}\n"
                f"• Людей: {lead['people']}\n"
                f"• Пожелания: {lead['notes']}"
            )
            await context.bot.send_message(chat_id=int(MANAGER_CHAT_ID), text=txt)
        except Exception as e:
            log.warning("Не смог отправить менеджеру: %s", e)

    await update.message.reply_text("Спасибо! 🙌 Я передал заявку менеджеру. Мы скоро свяжемся.")
    return ConversationHandler.END

async def st_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Опрос отменён. Введите /rent чтобы начать заново.", reply_markup=kb_remove())
    return ConversationHandler.END

# ---------- Канал ----------
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
    listings_append(item)
    log.info("Saved listing %s from @%s", listing_id, msg.chat.username)

# ---------- GPT fallback для обычных сообщений ----------
async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # не отвечаем, если внутри ConversationHandler (он перехватит)
    # сюда попадает текст вне анкеты и не команда
    text = update.message.text or ""
    ans = gpt_reply(text, (update.effective_user.username or ""))
    if ans:
        await update.message.reply_text(ans)
    else:
        # Если GPT не настроен — мягкий ответ-подсказка
        await update.message.reply_text("Готов помочь с подбором жилья! Можете написать ваш запрос свободным текстом или запустить анкету командой /rent.")

# ---------- Сборка приложения ----------
def build_app() -> Application:
    app: Application = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))

    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", cmd_rent)],
        states={
            AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_area)],
            BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_bedrooms)],
            BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_budget)],
            PETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_pets)],
            POOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_pool)],
            WORKSPACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_workspace)],
            PEOPLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_people)],
            PHONE: [
                MessageHandler(filters.CONTACT, st_phone),
                MessageHandler(filters.TEXT & ~filters.COMMAND, st_phone),
            ],
            NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_notes)],
        },
        fallbacks=[CommandHandler("cancel", st_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # посты канала
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & (filters.TEXT | filters.CAPTION), on_channel_post))

    # GPT для обычных текстов (последним, чтобы не мешать /rent)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))

    return app

# ---------- Режимы запуска ----------
async def run_webhook(app: Application) -> None:
    bot: Bot = app.bot
    await bot.delete_webhook(drop_pending_updates=True)
    webhook_url = f"{BASE_URL}{WEBHOOK_PATH if WEBHOOK_PATH.startswith('/') else '/'+WEBHOOK_PATH}"
    await bot.set_webhook(url=webhook_url, allowed_updates=["message", "channel_post", "callback_query"])
    log.info("Starting webhook at %s (PORT=%s)", webhook_url, os.environ.get("PORT","10000"))

    # страховка на Py3.12+/Render
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    await app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        url_path=WEBHOOK_PATH.lstrip("/"),
        webhook_url=webhook_url,
        allowed_updates=["message", "channel_post", "callback_query"],
    )

async def run_polling(app: Application) -> None:
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.initialize()
    await app.start()
    log.info("Polling started…")
    try:
        await app.updater.start_polling(allowed_updates=["message", "channel_post", "callback_query"])
        await app.updater.wait()
    finally:
        await app.stop()
        await app.shutdown()

def main() -> None:
    setup_gsheets()
    app = build_app()

    async def _run():
        me = await app.bot.get_me()
        log.info("Bot: @%s", me.username)
        if BASE_URL:
            await run_webhook(app)
        else:
            await run_polling(app)

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())

if __name__ == "__main__":
    main()
