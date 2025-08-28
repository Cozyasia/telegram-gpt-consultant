# -*- coding: utf-8 -*-
"""
Cozy Asia Bot — единый main.py
- Работает на Render (webhook) и локально (polling).
- Снимает старые вебхуки с drop_pending_updates=True (нет 409).
- Парсит посты канала и пишет лоты в Google Sheets (лист "Listings").
- Ведёт анкету /rent: район → спальни → бюджет, предлагает подходящие лоты.

Переменные окружения (точные имена):
  TELEGRAM_BOT_TOKEN   (обяз.)
  BASE_URL             (для Render, например https://telegram-gpt-consultant-xxxx.onrender.com)
  WEBHOOK_PATH         (/webhook — оставь так)
  PUBLIC_CHANNEL       (юзернейм канала без @, например samuirental)
  GREETING_MESSAGE     (необяз. текст приветствия)
  MANAGER_CHAT_ID      (необяз. чат ID менеджера, число)
  GOOGLE_SHEET_ID      (если пишем в таблицу)
  GOOGLE_CREDS_JSON    (весь JSON сервис-аккаунта)
  LOG_LEVEL            (INFO/DEBUG и т.п.)
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
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]  # обязательна
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")  # пусто = локальный polling
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
PUBLIC_CHANNEL = os.environ.get("PUBLIC_CHANNEL", "").lstrip("@").strip()
GREETING_MESSAGE = os.environ.get(
    "GREETING_MESSAGE",
    "Привет! Я ассистент Cozy Asia 🌴\nНапиши, что ищешь (район, бюджет, спальни, с питомцами и т.д.) "
    "или нажми /rent — подберу варианты из базы.",
)
MANAGER_CHAT_ID = os.environ.get("MANAGER_CHAT_ID", "").strip() or None

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "").strip()

# ---------- Google Sheets (опционально) ----------
gspread = None
sheet_works = False
worksheet = None
LISTINGS_SHEET_NAME = "Listings"
LISTING_COLUMNS = [
    "listing_id", "created_at", "title", "description", "location", "bedrooms",
    "bathrooms", "price_month", "pets_allowed", "utilities", "electricity_rate",
    "water_rate", "area_m2", "pool", "furnished", "link", "images", "tags", "raw_text"
]

def setup_gsheets_if_possible() -> None:
    """Подключение к Google Sheets, если заданы переменные."""
    global gspread, sheet_works, worksheet
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDS_JSON:
        log.info("Google Sheets не настроен — переменные не заданы.")
        return
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore

        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sh = client.open_by_key(GOOGLE_SHEET_ID)
        if LISTINGS_SHEET_NAME in [w.title for w in sh.worksheets()]:
            worksheet = sh.worksheet(LISTINGS_SHEET_NAME)
        else:
            worksheet = sh.add_worksheet(title=LISTINGS_SHEET_NAME, rows="1000", cols=str(len(LISTING_COLUMNS)))
            worksheet.append_row(LISTING_COLUMNS)
        sheet_works = True
        log.info("Google Sheets подключен, лист: %s", LISTINGS_SHEET_NAME)
    except Exception as e:
        log.exception("Не удалось подключиться к Google Sheets: %s", e)
        sheet_works = False

# В памяти держим лоты (для быстрых рекомендаций)
IN_MEMORY_LISTINGS: List[Dict[str, Any]] = []

def append_listing_row(row: Dict[str, Any]) -> None:
    """Записывает лот в таблицу и добавляет в память."""
    global worksheet, sheet_works
    IN_MEMORY_LISTINGS.append(row)
    if not sheet_works or worksheet is None:
        return
    try:
        values = [row.get(col, "") for col in LISTING_COLUMNS]
        worksheet.append_row(values, value_input_option="USER_ENTERED")
    except Exception as e:
        log.exception("Ошибка записи в Google Sheets: %s", e)

# ---------- Парсер постов ----------
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

def parse_listing_from_text(text: str, msg_link: str, listing_id: str) -> Dict[str, Any]:
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

    # price per month
    price = ""
    mp = re.search(r"(\d[\d\s]{3,})(?:\s*baht|\s*бат|\s*฿|b|thb)?", t)
    if mp:
        raw = mp.group(1)
        price = re.sub(r"\s", "", raw)

    # pets
    pets_allowed = "unknown"
    if "без питомц" in t or "no pets" in t:
        pets_allowed = "no"
    elif "с питомц" in t or "pets ok" in t or "pet friendly" in t:
        pets_allowed = "yes"

    # utilities
    utilities = "unknown"

    # pool/furnished
    pool = "yes" if ("pool" in t or "бассейн" in t) else "no"
    furnished = "yes" if ("furnished" in t or "мебел" in t) else "unknown"

    title = ""
    mt = re.search(r"^([^\n]{10,80})", text.strip())
    if mt:
        title = mt.group(1)

    row = {
        "listing_id": listing_id,
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        "title": title,
        "description": text[:2000],
        "location": location,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "price_month": price,
        "pets_allowed": pets_allowed,
        "utilities": utilities,
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
    return row

# ---------- Поиск подходящих лотов ----------
def suggest_listings(area: str, bedrooms: int, budget: int) -> List[Dict[str, Any]]:
    area_l = area.lower()
    res = []
    for item in IN_MEMORY_LISTINGS:
        ok_area = (area_l in (item.get("location") or "").lower()) if area_l else True
        try:
            bd = int(item.get("bedrooms") or 0)
        except Exception:
            bd = 0
        try:
            pr = int(item.get("price_month") or 0)
        except Exception:
            pr = 0
        if ok_area and (bd >= bedrooms or bd == 0) and (pr <= budget or pr == 0):
            res.append(item)
    # отфильтруем пустые/совсем неизвестные цены в конец
    res.sort(key=lambda x: int(x.get("price_month") or "99999999"))
    return res[:5]

# ---------- Обработчики ----------
AREA, BEDROOMS, BUDGET = range(3)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(GREETING_MESSAGE)

async def rent_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Начнём. Какой район Самуи предпочитаете? (например: Маенам, Бопхут, Чавенг, Ламай)")
    return AREA

async def rent_area(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["area"] = update.message.text.strip()
    await update.message.reply_text("Сколько спален нужно?")
    return BEDROOMS

async def rent_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = update.message.text.strip()
    m = re.search(r"\d+", txt)
    bedrooms = int(m.group(0)) if m else 1
    context.user_data["bedrooms"] = bedrooms
    await update.message.reply_text("Какой бюджет в месяц (бат)?")
    return BUDGET

async def rent_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = update.message.text.strip().replace(" ", "")
    m = re.search(r"\d+", txt)
    budget = int(m.group(0)) if m else 0
    context.user_data["budget"] = budget

    area = context.user_data.get("area", "")
    bedrooms = int(context.user_data.get("bedrooms", 1))
    budget = int(context.user_data.get("budget", 0))

    # рекомендации
    offers = suggest_listings(area, bedrooms, budget)
    if offers:
        lines = ["Нашёл подходящие варианты:"]
        for o in offers:
            line = f"• {o.get('title') or 'Лот'} — спальни: {o.get('bedrooms') or '?'}; цена: {o.get('price_month') or '?'}; район: {o.get('location') or '?'}\n{(o.get('link') or '')}"
            lines.append(line)
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

async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, отменил. Напишите /rent когда будете готовы.")
    return ConversationHandler.END

async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловим посты в канале, парсим и пишем в таблицу."""
    if not update.channel_post:
        return
    msg = update.channel_post
    uname = (msg.chat.username or "").lower()
    if PUBLIC_CHANNEL and uname != PUBLIC_CHANNEL.lower():
        # чужой канал — игнор
        return

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    message_link = ""
    if msg.link:
        message_link = msg.link
    elif msg.chat.username:
        message_link = f"https://t.me/{msg.chat.username}/{msg.message_id}"

    listing_id = f"{msg.chat.id}_{msg.message_id}"
    item = parse_listing_from_text(text, message_link, listing_id)
    append_listing_row(item)
    log.info("Сохранён лот %s из канала @%s", listing_id, msg.chat.username)

# ---------- Сборка приложения ----------
def build_application() -> Application:
    app: Application = ApplicationBuilder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start_cmd))

    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_start)],
        states={
            AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_area)],
            BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_bedrooms)],
            BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_budget)],
        },
        fallbacks=[CommandHandler("cancel", rent_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Посты канала
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, on_channel_post))

    return app

# ---------- Режимы запуска ----------
async def run_webhook(app: Application) -> None:
    """Запуск в режиме webhook (Render)."""
    bot: Bot = app.bot

    # Снимаем старый вебхук и чистим "висящие" апдейты
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("deleteWebhook -> OK")

    webhook_url = f"{BASE_URL}{WEBHOOK_PATH if WEBHOOK_PATH.startswith('/') else '/'+WEBHOOK_PATH}"
    # выставляем вебхук до запуска сервера
    await bot.set_webhook(url=webhook_url, allowed_updates=["message", "channel_post", "callback_query"])
    log.info('setWebhook -> %s', webhook_url)

    # Явно создаём event loop (исправляет "no current event loop" на Py3.12/3.13)
    loop = asyncio.get_event_loop()
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
    """Локальный запуск polling (BASE_URL не задан)."""
    await app.bot.delete_webhook(drop_pending_updates=True)
    log.info("deleteWebhook -> OK (polling mode)")
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
    setup_gsheets_if_possible()
    app = build_application()

    # Для логов
    async def _run() -> None:
        me = await app.bot.get_me()
        log.info("Bot started: @%s", me.username)

        if BASE_URL:
            await run_webhook(app)
        else:
            await run_polling(app)

    # Надёжный запуск корутин с явным циклом
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
