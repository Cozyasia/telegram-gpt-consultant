# -*- coding: utf-8 -*-
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

# ------------ ЛОГИ
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cozy_bot")

# ------------ ENV
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]                # обяз.
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")   # для Render; пусто = polling
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
PORT = int(os.environ.get("PORT", "10000"))

PUBLIC_CHANNEL = os.environ.get("PUBLIC_CHANNEL", "").lstrip("@").strip()
GREETING_MESSAGE = os.environ.get(
    "GREETING_MESSAGE",
    "Привет! Я ассистент Cozy Asia 🌴\nНапиши, что ищешь (район, бюджет, спальни, с питомцами и т.д.) "
    "или нажми /rent — подберу варианты из базы.",
)
MANAGER_CHAT_ID = os.environ.get("MANAGER_CHAT_ID", "").strip() or None

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "").strip()

# ------------ Google Sheets (опционально)
gspread = None
worksheet = None
sheet_ready = False

LISTINGS_SHEET_NAME = "Listings"
LISTING_COLUMNS = [
    "listing_id", "created_at", "title", "description", "location", "bedrooms",
    "bathrooms", "price_month", "pets_allowed", "utilities", "electricity_rate",
    "water_rate", "area_m2", "pool", "furnished", "link", "images", "tags", "raw_text"
]

def setup_sheets():
    global gspread, worksheet, sheet_ready
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDS_JSON:
        log.info("Google Sheets не настроен (нет GOOGLE_SHEET_ID/GOOGLE_CREDS_JSON).")
        return
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDS_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        client = gspread.authorize(creds)
        sh = client.open_by_key(GOOGLE_SHEET_ID)
        titles = [w.title for w in sh.worksheets()]
        if LISTINGS_SHEET_NAME in titles:
            worksheet = sh.worksheet(LISTINGS_SHEET_NAME)
        else:
            worksheet = sh.add_worksheet(title=LISTINGS_SHEET_NAME, rows="1000", cols=str(len(LISTING_COLUMNS)))
            worksheet.append_row(LISTING_COLUMNS)
        sheet_ready = True
        log.info("Google Sheets подключен: лист %s", LISTINGS_SHEET_NAME)
    except Exception as e:
        log.exception("Sheets ошибка подключения: %s", e)

IN_MEMORY_LISTINGS: List[Dict[str, Any]] = []

def save_listing(row: Dict[str, Any]):
    IN_MEMORY_LISTINGS.append(row)
    if not sheet_ready or not worksheet:
        return
    try:
        worksheet.append_row([row.get(c, "") for c in LISTING_COLUMNS], value_input_option="USER_ENTERED")
    except Exception as e:
        log.exception("Sheets append_row ошибка: %s", e)

# ------------ Парсер лота из текста
REGION_WORDS = [
    "lamai","lamaï","lamay","ламай",
    "bophut","bo phut","бопхут",
    "chaweng","чавенг",
    "maenam","маенам",
    "ban rak","bangrak","bang rak","банрак","банграк",
    "choeng mon","чоенг мон","чоэнг мон",
    "lipanoi","lipa noi","липа ной",
    "taling ngam","талинг ньгам","талиннгам"
]

def parse_listing(text: str, link: str, listing_id: str) -> Dict[str, Any]:
    t = text.lower()

    location = ""
    for w in REGION_WORDS:
        if w in t:
            location = w
            break

    m = re.search(r"(\d+)\s*(спальн|bed(room)?s?)", t)
    bedrooms = m.group(1) if m else ""

    mb = re.search(r"(\d+)\s*(ванн|bath(room)?s?)", t)
    bathrooms = mb.group(1) if mb else ""

    mp = re.search(r"(\d[\d\s]{3,})(?:\s*(?:baht|бат|฿|b|thb))?", t)
    price = re.sub(r"\s", "", mp.group(1)) if mp else ""

    pets = "unknown"
    if "no pets" in t or "без питомц" in t:
        pets = "no"
    elif "pet friendly" in t or "pets ok" in t or "с питомц" in t:
        pets = "yes"

    pool = "yes" if ("pool" in t or "бассейн" in t) else "no"
    furnished = "yes" if ("furnished" in t or "мебел" in t) else "unknown"

    mt = re.search(r"^([^\n]{10,80})", text.strip())
    title = mt.group(1) if mt else ""

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
        "link": link,
        "images": "",
        "tags": "",
        "raw_text": text,
    }

def suggest(area: str, bedrooms: int, budget: int) -> List[Dict[str, Any]]:
    a = area.lower()
    res = []
    for it in IN_MEMORY_LISTINGS:
        ok_area = a in (it.get("location") or "").lower() if a else True
        try:
            bd = int(it.get("bedrooms") or 0)
        except:
            bd = 0
        try:
            pr = int(it.get("price_month") or 0)
        except:
            pr = 0
        if ok_area and (bd >= bedrooms or bd == 0) and (pr <= budget or pr == 0):
            res.append(it)
    res.sort(key=lambda x: int(x.get("price_month") or "99999999"))
    return res[:5]

# ------------ Диалог /rent
AREA, BEDROOMS, BUDGET = range(3)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(GREETING_MESSAGE)

async def rent_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Начнём. Какой район Самуи предпочитаете? (например: Маенам, Бопхут, Чавенг, Ламай)")
    return AREA

async def rent_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["area"] = update.message.text.strip()
    await update.message.reply_text("Сколько спален нужно?")
    return BEDROOMS

async def rent_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.search(r"\d+", update.message.text or "")
    context.user_data["bedrooms"] = int(m.group(0)) if m else 1
    await update.message.reply_text("Какой бюджет в месяц (бат)?")
    return BUDGET

async def rent_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.search(r"\d+", (update.message.text or "").replace(" ", ""))
    context.user_data["budget"] = int(m.group(0)) if m else 0

    area = context.user_data.get("area", "")
    bedrooms = int(context.user_data.get("bedrooms", 1))
    budget = int(context.user_data.get("budget", 0))

    offers = suggest(area, bedrooms, budget)
    if offers:
        lines = ["Нашёл подходящие варианты:"]
        for o in offers:
            lines.append(
                f"• {o.get('title') or 'Лот'} — спальни: {o.get('bedrooms') or '?'}; цена: {o.get('price_month') or '?'}; "
                f"район: {o.get('location') or '?'}\n{o.get('link') or ''}"
            )
        await update.message.reply_text("\n\n".join(lines))
    else:
        await update.message.reply_text("Пока ничего не нашёл в базе по этим критериям. Я передам заявку менеджеру.")
        if MANAGER_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=int(MANAGER_CHAT_ID),
                    text=f"Заявка: район={area}; спальни={bedrooms}; бюджет={budget}; от пользователя {update.effective_user.id}",
                )
            except Exception as e:
                log.warning("Не удалось отправить менеджеру: %s", e)

    await update.message.reply_text("Если хотите, можем уточнить детали или начать заново: /rent")
    return ConversationHandler.END

async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок, отменил. Напишите /rent когда будете готовы.")
    return ConversationHandler.END

# ------------ Приём постов канала
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
    listing_id = f"{msg.chat.id}_{msg.message_id}"
    item = parse_listing(text, link, listing_id)
    save_listing(item)
    log.info("Сохранён лот %s из канала @%s", listing_id, msg.chat.username)

# ------------ Сборка и запуск
def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))

    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_entry)],
        states={
            AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_area)],
            BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_bedrooms)],
            BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_budget)],
        },
        fallbacks=[CommandHandler("cancel", rent_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & (filters.TEXT | filters.Caption()), on_channel_post))
    return app

def main():
    setup_sheets()
    app = build_app()

    # Снимем старый вебхук с очисткой очереди (убеждаемся, что нет 409)
    # PTB сам поставит новый вебхук внутри run_webhook.
    app.bot.delete_webhook(drop_pending_updates=True)

    if BASE_URL:
        webhook_url = f"{BASE_URL}{WEBHOOK_PATH if WEBHOOK_PATH.startswith('/') else '/'+WEBHOOK_PATH}"
        log.info("Starting webhook at %s (PORT=%s)", webhook_url, PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH.lstrip("/"),
            webhook_url=webhook_url,
            allowed_updates=["message", "channel_post", "callback_query"],
        )
    else:
        log.info("Starting polling…")
        app.run_polling(allowed_updates=["message", "channel_post", "callback_query"])

if __name__ == "__main__":
    main()
