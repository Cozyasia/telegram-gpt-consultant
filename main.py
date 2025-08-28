# -*- coding: utf-8 -*-
"""
Cozy Asia Bot — единый main.py (PTB 21.x)

Переменные окружения (точные имена):
  TELEGRAM_BOT_TOKEN   — токен бота (обяз.)
  BASE_URL             — внешний URL Render без слеша на конце (напр. https://your-app.onrender.com)
  WEBHOOK_PATH         — путь вебхука, по умолчанию /webhook
  PUBLIC_CHANNEL       — юзернейм канала без @ (напр. samuirental)
  GREETING_MESSAGE     — приветствие (необяз.)
  MANAGER_CHAT_ID      — chat_id менеджера (целое число, можно 0/пусто)
  GOOGLE_SHEET_ID      — ID таблицы (если хотим писать в Sheets)
  GOOGLE_CREDS_JSON    — JSON сервис-аккаунта (целиком, одной строкой)
  LOG_LEVEL            — INFO/DEBUG/…
"""

import os
import re
import json
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from telegram import Update, Bot
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- ЛОГИ ----------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cozy_bot")

# ---------- ENV ----------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")            # пусто => локальный polling
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
PUBLIC_CHANNEL = os.environ.get("PUBLIC_CHANNEL", "").lstrip("@").strip()

GREETING_MESSAGE = os.environ.get(
    "GREETING_MESSAGE",
    "Привет! Я ассистент Cozy Asia 🌴\nНапиши, что ищешь (район, бюджет, спальни, с питомцами и т.д.) "
    "или нажми /rent — подберу варианты из базы.",
)

MANAGER_CHAT_ID: Optional[int]
_man = os.environ.get("MANAGER_CHAT_ID", "").strip()
MANAGER_CHAT_ID = int(_man) if _man.isdigit() else None

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "").strip()

# ---------- Google Sheets (опционально) ----------
gspread = None
sheet_ok = False
ws_listings = None
ws_requests = None

LISTINGS_SHEET_NAME = "Listings"
LISTINGS_COLS = [
    "listing_id", "created_at", "title", "description", "location", "bedrooms",
    "bathrooms", "price_month", "pets_allowed", "utilities", "electricity_rate",
    "water_rate", "area_m2", "pool", "furnished", "link", "images", "tags", "raw_text"
]

REQUESTS_SHEET_NAME = "Requests"
REQUESTS_COLS = [
    "request_id", "created_at", "user_id", "username",
    "area", "bedrooms", "budget", "pets", "guests", "notes", "matched_count"
]

def setup_gsheets() -> None:
    """Подключаемся к Google Sheets, если заданы переменные."""
    global gspread, sheet_ok, ws_listings, ws_requests
    if not (GOOGLE_SHEET_ID and GOOGLE_CREDS_JSON):
        log.info("Google Sheets не настроен — переменные не заданы.")
        return
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore

        creds = json.loads(GOOGLE_CREDS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        gc = gspread.authorize(
            Credentials.from_service_account_info(creds, scopes=scopes)
        )
        sh = gc.open_by_key(GOOGLE_SHEET_ID)

        # Listings
        titles = [w.title for w in sh.worksheets()]
        if LISTINGS_SHEET_NAME in titles:
            ws_listings = sh.worksheet(LISTINGS_SHEET_NAME)
        else:
            ws_listings = sh.add_worksheet(
                title=LISTINGS_SHEET_NAME, rows="1000", cols=str(len(LISTINGS_COLS))
            )
            ws_listings.append_row(LISTINGS_COLS)

        # Requests
        titles = [w.title for w in sh.worksheets()]
        if REQUESTS_SHEET_NAME in titles:
            ws_requests = sh.worksheet(REQUESTS_SHEET_NAME)
        else:
            ws_requests = sh.add_worksheet(
                title=REQUESTS_SHEET_NAME, rows="1000", cols=str(len(REQUESTS_COLS))
            )
            ws_requests.append_row(REQUESTS_COLS)

        sheet_ok = True
        log.info("Google Sheets подключен: листы '%s' и '%s'.", LISTINGS_SHEET_NAME, REQUESTS_SHEET_NAME)
    except Exception as e:
        sheet_ok = False
        log.exception("Не удалось подключиться к Google Sheets: %s", e)

def append_listings_row(row: Dict[str, Any]) -> None:
    """Запись лота в лист Listings + в память."""
    IN_MEMORY_LISTINGS.append(row)
    if sheet_ok and ws_listings:
        try:
            ws_listings.append_row([row.get(c, "") for c in LISTINGS_COLS], value_input_option="USER_ENTERED")
        except Exception as e:
            log.warning("Не удалось записать лот в Sheets: %s", e)

def append_request_row(row: Dict[str, Any]) -> None:
    """Запись заявки в лист Requests."""
    if sheet_ok and ws_requests:
        try:
            ws_requests.append_row([row.get(c, "") for c in REQUESTS_COLS], value_input_option="USER_ENTERED")
        except Exception as e:
            log.warning("Не удалось записать заявку в Sheets: %s", e)

# ---------- Память с лотами ----------
IN_MEMORY_LISTINGS: List[Dict[str, Any]] = []

# ---------- Парсер постов из канала ----------
REGION_WORDS = [
    "lamai","lamaï","lamay","ламай",
    "bophut","bo phut","бопхут",
    "chaweng","чавенг",
    "maenam","маенам",
    "ban rak","bangrak","bang rak","банрак","банграк",
    "choeng mon","чоенг мон","чоэнг мон",
    "lipa noi","lipanoi","липа ной",
    "taling ngam","талинг ньгам","талиннгам"
]

def parse_listing_text(text: str, msg_link: str, listing_id: str) -> Dict[str, Any]:
    t = text.lower()

    # location
    location = ""
    for w in REGION_WORDS:
        if w in t:
            location = w
            break

    # bedrooms
    bedrooms = ""
    m = re.search(r"(\d+)\s*(спальн|bed(room)?s?)", t)
    if m:
        bedrooms = m.group(1)

    # bathrooms
    bathrooms = ""
    mb = re.search(r"(\d+)\s*(ванн|bath(room)?s?)", t)
    if mb:
        bathrooms = mb.group(1)

    # monthly price
    price = ""
    mp = re.search(r"(\d[\d\s]{3,})(?:\s*(?:baht|бат|฿|b|thb))?", t)
    if mp:
        price = re.sub(r"\s+", "", mp.group(1))

    pets = "unknown"
    if "без питомц" in t or "no pets" in t:
        pets = "no"
    elif "с питомц" in t or "pets ok" in t or "pet friendly" in t:
        pets = "yes"

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

# ---------- Подбор вариантов ----------
def suggest_listings(area: str, bedrooms: int, budget: int) -> List[Dict[str, Any]]:
    area_l = (area or "").lower().strip()
    res: List[Dict[str, Any]] = []
    for it in IN_MEMORY_LISTINGS:
        ok_area = True if not area_l else area_l in (it.get("location") or "").lower()
        try:
            bd = int(it.get("bedrooms") or 0)
        except Exception:
            bd = 0
        try:
            pr = int(it.get("price_month") or 0)
        except Exception:
            pr = 0
        if ok_area and (bd >= bedrooms or bd == 0) and (budget == 0 or pr == 0 or pr <= budget):
            res.append(it)
    res.sort(key=lambda x: int(x.get("price_month") or 10**9))
    return res[:5]

# ---------- Диалог /rent ----------
AREA, BEDROOMS, BUDGET, PETS, GUESTS, NOTES = range(6)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(GREETING_MESSAGE)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, отменил. Если что — /rent для нового запроса.")
    return ConversationHandler.END

async def rent_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Начнём. Какой район Самуи предпочитаете? (например: Маенам, Бопхут, Чавенг, Ламай)"
    )
    return AREA

async def rent_area(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["area"] = (update.message.text or "").strip()
    await update.message.reply_text("Сколько спален нужно?")
    return BEDROOMS

async def rent_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = re.search(r"\d+", (update.message.text or ""))
    context.user_data["bedrooms"] = int(m.group()) if m else 1
    await update.message.reply_text("Какой бюджет в месяц (бат)?")
    return BUDGET

async def rent_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = (update.message.text or "").replace(" ", "")
    m = re.search(r"\d+", s)
    context.user_data["budget"] = int(m.group()) if m else 0
    await update.message.reply_text("С питомцами? (да/нет)")
    return PETS

async def rent_pets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = (update.message.text or "").strip().lower()
    context.user_data["pets"] = "yes" if txt in ("да","yes","y","+") else ("no" if txt in ("нет","no","n","-") else "unknown")
    await update.message.reply_text("Сколько человек будет проживать?")
    return GUESTS

async def rent_guests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = re.search(r"\d+", (update.message.text or ""))
    context.user_data["guests"] = int(m.group()) if m else 1
    await update.message.reply_text("Есть ли особые пожелания? (например: бассейн, вид на море, рядом с школой).")
    return NOTES

async def rent_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["notes"] = (update.message.text or "").strip()

    # Итоги
    area = context.user_data.get("area","")
    bedrooms = int(context.user_data.get("bedrooms",1))
    budget = int(context.user_data.get("budget",0))
    pets = context.user_data.get("pets","unknown")
    guests = int(context.user_data.get("guests",1))
    notes = context.user_data.get("notes","")

    # Подбор
    offers = suggest_listings(area, bedrooms, budget)

    # Ответ пользователю
    if offers:
        lines = ["Нашёл подходящие варианты:"]
        for o in offers:
            line = (
                f"• {o.get('title') or 'Лот'} — спален: {o.get('bedrooms') or '?'}, "
                f"цена: {o.get('price_month') or '?'}, район: {o.get('location') or '?'}\n"
                f"{o.get('link') or ''}"
            )
            lines.append(line)
        await update.message.reply_text("\n\n".join(lines))
    else:
        await update.message.reply_text("Пока ничего не нашёл по этим критериям. Я передам заявку менеджеру.")

    # Запись в Requests
    req_row = {
        "request_id": f"{update.effective_user.id}_{int(datetime.utcnow().timestamp())}",
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        "user_id": update.effective_user.id if update.effective_user else "",
        "username": (update.effective_user.username if update.effective_user else "") or "",
        "area": area, "bedrooms": bedrooms, "budget": budget,
        "pets": pets, "guests": guests, "notes": notes,
        "matched_count": len(offers),
    }
    append_request_row(req_row)

    # Уведомление менеджеру
    if MANAGER_CHAT_ID:
        try:
            text = (
                "Новая заявка:\n"
                f"Район: {area}\nСпален: {bedrooms}\nБюджет: {budget}\nПитомцы: {pets}\n"
                f"Жильцы: {guests}\nПожелания: {notes}\n"
                f"Пользователь: @{(update.effective_user.username or '—')}"
            )
            await context.bot.send_message(chat_id=MANAGER_CHAT_ID, text=text)
        except Exception as e:
            log.warning("Не удалось отправить менеджеру: %s", e)

    await update.message.reply_text("Спасибо! Если хотите, можем уточнить детали или начать заново: /rent")
    return ConversationHandler.END

# ---------- Посты канала ----------
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.channel_post
    if not msg:
        return

    uname = (msg.chat.username or "").lower()
    if PUBLIC_CHANNEL and uname != PUBLIC_CHANNEL.lower():
        return  # не наш канал

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    # Ссылка на сообщение
    if msg.chat.username:
        link = f"https://t.me/{msg.chat.username}/{msg.message_id}"
    else:
        link = ""

    listing_id = f"{msg.chat.id}_{msg.message_id}"
    row = parse_listing_text(text, link, listing_id)
    append_listings_row(row)
    log.info("Сохранён лот %s из канала @%s", listing_id, msg.chat.username)

# ---------- Сборка приложения ----------
def make_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    # /start, /rent, /cancel
    app.add_handler(CommandHandler("start", cmd_start))

    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_start)],
        states={
            AREA:      [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, rent_area)],
            BEDROOMS:  [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, rent_bedrooms)],
            BUDGET:    [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, rent_budget)],
            PETS:      [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, rent_pets)],
            GUESTS:    [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, rent_guests)],
            NOTES:     [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, rent_notes)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Посты канала
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & (filters.TEXT | filters.CAPTION), on_channel_post))

    return app

# ---------- Режимы запуска ----------
async def run_webhook(app: Application) -> None:
    """Запуск на Render (вебхук). Никаких ручных манипуляций с loop."""
    webhook_url = f"{BASE_URL}{WEBHOOK_PATH if WEBHOOK_PATH.startswith('/') else '/'+WEBHOOK_PATH}"
    port = int(os.environ.get("PORT", "10000"))

    log.info("Starting webhook at %s (PORT=%s)", webhook_url, port)

    await app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=WEBHOOK_PATH.lstrip("/"),
        webhook_url=webhook_url,
        allowed_updates=("message", "channel_post", "callback_query"),
        drop_pending_updates=True,   # чистим очередь и избегаем 409
    )

async def run_polling(app: Application) -> None:
    """Локальный запуск (polling)."""
    await app.run_polling(
        allowed_updates=("message", "channel_post", "callback_query"),
        drop_pending_updates=True,
    )

def main() -> None:
    setup_gsheets()
    app = make_app()

    async def _run():
        me = await app.bot.get_me()
        log.info("Bot started: @%s", me.username)
        if BASE_URL:
            await run_webhook(app)
        else:
            await run_polling(app)

    # Современный и безопасный запуск без ручного закрытия loop
    asyncio.run(_run())

if __name__ == "__main__":
    main()
