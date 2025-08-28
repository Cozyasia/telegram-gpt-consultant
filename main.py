# -*- coding: utf-8 -*-
"""
Cozy Asia Bot — единый main.py

Функции:
- Работает на Render по webhook (если задан BASE_URL) и локально по polling (если BASE_URL пуст).
- Снимает старые вебхуки с drop_pending_updates=True, чтобы не ловить 409/503.
- Парсит посты указанного канала и пишет лоты в Google Sheets (лист "Listings"), а также кэширует их в памяти для рекомендаций.
- Анкета /rent: район → спальни → бюджет → питомцы → бассейн → рабочее место → кол-во жильцов → телефон → свободный текст.
  После анкеты пишет лид в лист "Leads", присылает резюме менеджеру (если указан MANAGER_CHAT_ID) и даёт рекомендации из кэша.
- GPT-ответы на любые НЕкомандные сообщения вне анкеты (нужно OPENAI_API_KEY; см. конец файла).

Переменные окружения:
  TELEGRAM_BOT_TOKEN   (обяз.)
  BASE_URL             (например https://telegram-gpt-consultant-xxxx.onrender.com)
  WEBHOOK_PATH         (по умолчанию /webhook)
  PUBLIC_CHANNEL       (юзернейм канала без @, например samuirental)
  GREETING_MESSAGE     (текст приветствия)
  MANAGER_CHAT_ID      (чат ID менеджера; число)
  GOOGLE_SHEET_ID      (ID таблицы)
  GOOGLE_CREDS_JSON    (JSON сервис-аккаунта одной строкой)
  LOG_LEVEL            (INFO/DEBUG/…)
  OPENAI_API_KEY       (для GPT-ответов)
  OPENAI_MODEL         (опционально; по умолчанию gpt-4o-mini)
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

# ------------------------------ ЛОГИ ------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cozy_bot")

# ------------------------------ ENV ------------------------------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]  # обязательна
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")  # пусто = локальный polling
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
PUBLIC_CHANNEL = os.environ.get("PUBLIC_CHANNEL", "").lstrip("@").strip()

GREETING_MESSAGE = os.environ.get(
    "GREETING_MESSAGE",
    "Привет! Я ассистент Cozy Asia 🌴\n"
    "Напиши, что ищешь (район, бюджет, спальни, пожелания и т.д.) или нажми /rent — подберу варианты.",
)
MANAGER_CHAT_ID = os.environ.get("MANAGER_CHAT_ID", "").strip() or None

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "").strip()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()

# ------------------------------ Google Sheets ------------------------------
gspread = None
sheet_ok = False
ws_leads = None
ws_listings = None

LEADS_SHEET = "Leads"
LISTINGS_SHEET = "Listings"

LEADS_COLUMNS = [
    "created_at", "chat_id", "username", "phone",
    "location", "budget", "bedrooms", "tenants",
    "pets", "pool", "workspace", "extra",
    "source", "status",
]

LISTING_COLUMNS = [
    "listing_id", "created_at", "title", "description", "location", "bedrooms",
    "bathrooms", "price_month", "pets_allowed", "utilities", "electricity_rate",
    "water_rate", "area_m2", "pool", "furnished", "link", "images", "tags", "raw_text"
]

def setup_gsheets_if_possible() -> None:
    """Подключение к Google Sheets, если заданы переменные."""
    global gspread, sheet_ok, ws_leads, ws_listings
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDS_JSON:
        log.info("Sheets не настроен (нет GOOGLE_SHEET_ID / GOOGLE_CREDS_JSON).")
        sheet_ok = False
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
        titles = [w.title for w in sh.worksheets()]
        if LEADS_SHEET in titles:
            ws_leads = sh.worksheet(LEADS_SHEET)
        else:
            ws_leads = sh.add_worksheet(title=LEADS_SHEET, rows="1000", cols=str(len(LEADS_COLUMNS)))
            ws_leads.append_row(LEADS_COLUMNS)

        # Listings
        if LISTINGS_SHEET in titles:
            ws_listings = sh.worksheet(LISTINGS_SHEET)
        else:
            ws_listings = sh.add_worksheet(title=LISTINGS_SHEET, rows="1000", cols=str(len(LISTING_COLUMNS)))
            ws_listings.append_row(LISTING_COLUMNS)

        sheet_ok = True
        log.info("Sheets подключен: листы '%s' и '%s'", LEADS_SHEET, LISTINGS_SHEET)
    except Exception as e:
        log.exception("Не удалось подключиться к Google Sheets: %s", e)
        sheet_ok = False

def leads_append(row: Dict[str, Any]) -> None:
    if not (sheet_ok and ws_leads):
        return
    try:
        values = [row.get(col, "") for col in LEADS_COLUMNS]
        ws_leads.append_row(values, value_input_option="USER_ENTERED")
    except Exception as e:
        log.exception("Ошибка записи лида в Sheets: %s", e)

def listings_append(row: Dict[str, Any]) -> None:
    if not (sheet_ok and ws_listings):
        return
    try:
        values = [row.get(col, "") for col in LISTING_COLUMNS]
        ws_listings.append_row(values, value_input_option="USER_ENTERED")
    except Exception as e:
        log.exception("Ошибка записи лота в Sheets: %s", e)

# ------------------------------ Кэш лотов ------------------------------
IN_MEMORY_LISTINGS: List[Dict[str, Any]] = []

# ------------------------------ Парсер постов ------------------------------
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
        "link": msg_link,
        "images": "",
        "tags": "",
        "raw_text": text,
    }

def suggest_listings(area: str, bedrooms: int, budget: int) -> List[Dict[str, Any]]:
    area_l = (area or "").lower()
    res: List[Dict[str, Any]] = []
    for it in IN_MEMORY_LISTINGS:
        ok_area = area_l in (it.get("location") or "").lower() if area_l else True
        try:
            bd = int(it.get("bedrooms") or 0)
        except Exception:
            bd = 0
        try:
            pr = int(it.get("price_month") or 0)
        except Exception:
            pr = 0
        if ok_area and (bd >= bedrooms or bd == 0) and (pr <= budget or pr == 0):
            res.append(it)
    res.sort(key=lambda x: int(x.get("price_month") or "99999999"))
    return res[:5]

# ------------------------------ Анкета ------------------------------
(
    AREA, BEDROOMS, BUDGET, PETS, POOL, WORKSPACE, TENANTS, PHONE, EXTRA
) = range(9)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(GREETING_MESSAGE)

async def rent_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("В каком районе Самуи хотите жить? (Маенам, Бопхут, Чавенг, Ламай…)")
    return AREA

async def rent_area(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["area"] = update.message.text.strip()
    await update.message.reply_text("Сколько спален нужно?")
    return BEDROOMS

async def rent_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = re.search(r"\d+", update.message.text)
    context.user_data["bedrooms"] = int(m.group()) if m else 1
    await update.message.reply_text("Какой бюджет в месяц (бат)?")
    return BUDGET

async def rent_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = re.search(r"\d+", update.message.text.replace(" ", ""))
    context.user_data["budget"] = int(m.group()) if m else 0
    await update.message.reply_text("Питомцы? (да/нет/неважно)")
    return PETS

async def rent_pets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.lower()
    context.user_data["pets"] = "yes" if "да" in t else "no" if "нет" in t else "any"
    await update.message.reply_text("Нужен бассейн? (да/нет/неважно)")
    return POOL

async def rent_pool(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.lower()
    context.user_data["pool"] = "yes" if "да" in t else "no" if "нет" in t else "any"
    await update.message.reply_text("Нужно рабочее место/стол? (да/нет/неважно)")
    return WORKSPACE

async def rent_workspace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.lower()
    context.user_data["workspace"] = "yes" if "да" in t else "no" if "нет" in t else "any"
    await update.message.reply_text("Сколько человек будет проживать?")
    return TENANTS

async def rent_tenants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = re.search(r"\d+", update.message.text)
    context.user_data["tenants"] = int(m.group()) if m else 1
    await update.message.reply_text("Оставьте телефон/телеграм для связи:")
    return PHONE

async def rent_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["phone"] = update.message.text.strip()
    await update.message.reply_text("Есть ли дополнительные пожелания? (например: вид на море, кухня, детская и т.д.)")
    return EXTRA

async def rent_extra(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["extra"] = update.message.text.strip()

    # Сохраняем лид
    user = update.effective_user
    row = {
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        "chat_id": str(user.id),
        "username": f"@{user.username}" if user.username else (user.full_name or ""),
        "phone": context.user_data.get("phone", ""),
        "location": context.user_data.get("area", ""),
        "budget": context.user_data.get("budget", ""),
        "bedrooms": context.user_data.get("bedrooms", ""),
        "tenants": context.user_data.get("tenants", ""),
        "pets": context.user_data.get("pets", ""),
        "pool": context.user_data.get("pool", ""),
        "workspace": context.user_data.get("workspace", ""),
        "extra": context.user_data.get("extra", ""),
        "source": "telegram",
        "status": "new",
    }
    leads_append(row)

    # Рекомендации
    offers = suggest_listings(
        area=row["location"],
        bedrooms=int(row["bedrooms"] or 0),
        budget=int(row["budget"] or 0),
    )
    if offers:
        lines = ["Нашёл подходящие варианты:"]
        for o in offers:
            line = (
                f"• {o.get('title') or 'Лот'} — спален: {o.get('bedrooms') or '?'}; "
                f"цена: {o.get('price_month') or '?'}; район: {o.get('location') or '?'}\n"
                f"{o.get('link') or ''}"
            )
            lines.append(line)
        await update.message.reply_text("\n\n".join(lines))
    else:
        await update.message.reply_text("Спасибо! 🙌 Я передал заявку менеджеру. Мы скоро свяжемся.")

    # Уведомление менеджеру
    if MANAGER_CHAT_ID:
        try:
            txt = (
                "Новый лид:\n"
                f"Район: {row['location']}\n"
                f"Спальни: {row['bedrooms']}\n"
                f"Бюджет: {row['budget']}\n"
                f"Жильцов: {row['tenants']}\n"
                f"Питомцы: {row['pets']}; Бассейн: {row['pool']}; Workspace: {row['workspace']}\n"
                f"Пожелания: {row['extra']}\n"
                f"Телефон: {row['phone']}\n"
                f"Пользователь: {row['username']} (id={row['chat_id']})"
            )
            await context.bot.send_message(chat_id=int(MANAGER_CHAT_ID), text=txt)
        except Exception as e:
            log.warning("Не удалось уведомить менеджера: %s", e)

    await update.message.reply_text("Если хотите, начнём новую заявку: /rent")
    return ConversationHandler.END

async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Опрос отменён. Введите /rent чтобы начать заново.")
    return ConversationHandler.END

# ------------------------------ Канал: приём постов ------------------------------
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

    message_link = ""
    if getattr(msg, "link", None):
        message_link = msg.link
    elif msg.chat.username:
        message_link = f"https://t.me/{msg.chat.username}/{msg.message_id}"

    listing_id = f"{msg.chat.id}_{msg.message_id}"
    item = parse_listing_from_text(text, message_link, listing_id)
    IN_MEMORY_LISTINGS.append(item)
    listings_append(item)
    log.info("Сохранён лот %s из @%s", listing_id, msg.chat.username)

# ------------------------------ GPT small talk ------------------------------
_openai_client = None
def _get_openai_client():
    global _openai_client
    if _openai_client or not OPENAI_API_KEY:
        return _openai_client
    try:
        from openai import OpenAI  # type: ignore
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        log.warning("OpenAI client init failed: %s", e)
        _openai_client = None
    return _openai_client

async def gpt_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отвечаем GPT на любые обычные сообщения (вне анкеты/команд)."""
    text = (update.message.text or "").strip()
    if not text:
        return

    client = _get_openai_client()
    if not client:
        await update.message.reply_text(
            "Я здесь! Чтобы подобрать жильё, нажмите /rent или напишите, что ищете 🙂"
        )
        return

    try:
        # Короткая системная роль, чтобы держать стиль
        msgs = [
            {"role": "system", "content": (
                "Ты дружелюбный русскоязычный ассистент агентства аренды на Самуи. "
                "Отвечай кратко и по делу. Предлагай команду /rent для подбора вариантов. "
                "Если спрашивают про районы/цены/условия — делай полезные пояснения."
            )},
            {"role": "user", "content": text},
        ]
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=msgs,
            temperature=0.4,
        )
        answer = resp.choices[0].message.content.strip()
        if answer:
            await update.message.reply_text(answer)
        else:
            await update.message.reply_text("Готов помочь с подбором! Нажмите /rent.")
    except Exception as e:
        log.warning("OpenAI error: %s", e)
        await update.message.reply_text("Готов помочь с подбором! Нажмите /rent.")

# ------------------------------ Сборка приложения ------------------------------
def build_application() -> Application:
    app: Application = ApplicationBuilder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start_cmd))

    # Анкета
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_start)],
        states={
            AREA:       [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_area)],
            BEDROOMS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_bedrooms)],
            BUDGET:     [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_budget)],
            PETS:       [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_pets)],
            POOL:       [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_pool)],
            WORKSPACE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_workspace)],
            TENANTS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_tenants)],
            PHONE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_phone)],
            EXTRA:      [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_extra)],
        },
        fallbacks=[CommandHandler("cancel", rent_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Посты из канала
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & (filters.TEXT | filters.PHOTO), on_channel_post))

    # GPT-ответы на обычные сообщения (в самом конце, чтобы не перехватывать состояния анкеты)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, gpt_reply))

    return app

# ------------------------------ Режимы запуска ------------------------------
async def run_webhook(app: Application) -> None:
    """Запуск в режиме webhook (Render)."""
    await app.bot.delete_webhook(drop_pending_updates=True)
    webhook_url = f"{BASE_URL}{WEBHOOK_PATH if WEBHOOK_PATH.startswith('/') else '/' + WEBHOOK_PATH}"
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
    await app.initialize()
    await app.start()
    log.info("Polling started…")
    try:
        await app.updater.start_polling(allowed_updates=["message", "channel_post", "callback_query"])
        await app.updater.wait()
    finally:
        await app.stop()
        await app.shutdown()

# ------------------------------ Точка входа ------------------------------
async def main_async() -> None:
    setup_gsheets_if_possible()
    app = build_application()

    me = await app.bot.get_me()
    uname = getattr(me, "username", None) or me.first_name or "-"
    log.info("Bot online: @%s (id=%s)", uname, me.id)

    if BASE_URL:
        log.info("Starting webhook at %s%s (PORT=%s)", BASE_URL, WEBHOOK_PATH, os.environ.get("PORT", "10000"))
        await run_webhook(app)
    else:
        await run_polling(app)

if __name__ == "__main__":
    asyncio.run(main_async())
