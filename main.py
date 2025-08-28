# -*- coding: utf-8 -*-
"""
Cozy Asia Bot — единый main.py (PTB v21)
- Webhook (Render) ИЛИ polling локально (без ручного управления asyncio).
- GPT small talk вне опроса (/rent) — если есть OPENAI_API_KEY.
- Анкета /rent: район → спальни → бюджет → люди → питомцы → бассейн → рабочее место → телефон → имя → пожелания.
- Запись лида в Google Sheets (лист 'Leads'), лотов из канала — в 'Listings'.

ENV (точные имена):
  TELEGRAM_BOT_TOKEN   (обяз.)
  BASE_URL             (для Render, напр. https://telegram-gpt-consultant-xxxx.onrender.com)
  WEBHOOK_PATH         (по умолчанию /webhook)
  PUBLIC_CHANNEL       (юзернейм канала без @)
  GREETING_MESSAGE     (необяз.)
  MANAGER_CHAT_ID      (необяз., int)
  OPENAI_API_KEY       (если хотите GPT)
  GPT_MODEL            (по умолчанию gpt-4o-mini)

  GOOGLE_SHEET_ID      (для Sheets)
  GOOGLE_CREDS_JSON    (полный JSON сервис-аккаунта)
  LOG_LEVEL            (INFO/DEBUG)
"""

import os
import re
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ConversationHandler,
    MessageHandler, ContextTypes, filters
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

# Приветствие по умолчанию — можно переопределить переменной GREETING_MESSAGE
DEFAULT_GREETING = (
    "👋 Привет! Добро пожаловать в «Cosy Asia Real Estate Bot»\n\n"
    "😊 Я твой ИИ помощник и консультант.\n"
    "🗣️ Со мной можно говорить так же свободно, как с человеком.\n\n"
    "❓ Задавай любые вопросы:\n"
    "🏡 про дома, виллы и квартиры на Самуи\n"
    "🌴 про жизнь на острове, районы и атмосферу, погоду на время пребывания\n"
    "🍹 про быт, отдых и всё, что тебе интересно, куда сходить на острове 🏝️\n\n"
    "✨ Я всегда рядом, чтобы найти лучшее жильё и чувствовать себя на Самуи как дома 🏖️\n"
    "Помогу в любой момент.\n\n"
    "👉 Или нажми /rent — задам пару вопросов, сформирую заявку и передам менеджеру."
)
GREETING_MESSAGE = os.environ.get("GREETING_MESSAGE", DEFAULT_GREETING)

MANAGER_CHAT_ID = os.environ.get("MANAGER_CHAT_ID", "").strip() or None

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
GPT_MODEL = os.environ.get("GPT_MODEL", "gpt-4o-mini")

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "").strip()

# ---------- Google Sheets (опционально) ----------
gspread = None
sheet_works = False
ws_leads = None
ws_listings = None

LEADS_SHEET = "Leads"
LEAD_COLUMNS = [
    "created_at", "chat_id", "username",
    "location", "bedrooms", "budget",
    "people", "pets", "pool", "workspace",
    "phone", "name", "notes"
]

LISTINGS_SHEET = "Listings"
LISTING_COLUMNS = [
    "listing_id", "created_at", "title", "description", "location", "bedrooms",
    "bathrooms", "price_month", "pets_allowed", "utilities", "electricity_rate",
    "water_rate", "area_m2", "pool", "furnished", "link", "images", "tags", "raw_text"
]

def _ensure_ws(sh, title: str, headers: List[str]):
    if title in [w.title for w in sh.worksheets()]:
        ws = sh.worksheet(title)
        try:
            first_row = ws.row_values(1)
            if first_row != headers:
                ws.update('1:1', [headers])
        except Exception:
            pass
        return ws
    ws = sh.add_worksheet(title=title, rows="1000", cols=str(len(headers)))
    ws.append_row(headers)
    return ws

def setup_gsheets_if_possible() -> None:
    global gspread, sheet_works, ws_leads, ws_listings
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDS_JSON:
        log.info("Sheets: переменные не заданы — пропускаю.")
        return
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sh = client.open_by_key(GOOGLE_SHEET_ID)
        ws_leads = _ensure_ws(sh, LEADS_SHEET, LEAD_COLUMNS)
        ws_listings = _ensure_ws(sh, LISTINGS_SHEET, LISTING_COLUMNS)
        sheet_works = True
        log.info("Sheets подключены: '%s' и '%s'", LEADS_SHEET, LISTINGS_SHEET)
    except Exception as e:
        log.exception("Sheets: не удалось подключиться: %s", e)
        sheet_works = False

def leads_append(row: Dict[str, Any]) -> None:
    if not sheet_works or ws_leads is None:
        return
    try:
        ws_leads.append_row([row.get(c, "") for c in LEAD_COLUMNS], value_input_option="USER_ENTERED")
    except Exception as e:
        log.warning("Sheets: не смог записать лид: %s", e)

def listings_append(row: Dict[str, Any]) -> None:
    if not sheet_works or ws_listings is None:
        return
    try:
        ws_listings.append_row([row.get(c, "") for c in LISTING_COLUMNS], value_input_option="USER_ENTERED")
    except Exception as e:
        log.warning("Sheets: не смог записать лот: %s", e)

# ---------- Память лотов (для быстрых рекомендаций) ----------
IN_MEMORY_LISTINGS: List[Dict[str, Any]] = []

# ---------- Парсер постов канала ----------
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
    location = ""
    for w in REGION_WORDS:
        if w in t:
            location = w
            break

    # bedrooms
    m = re.search(r"(\d+)\s*(спальн|bed(room)?s?)", t)
    bedrooms = m.group(1) if m else ""

    # bathrooms
    mb = re.search(r"(\d+)\s*(ванн|bath(room)?s?)", t)
    bathrooms = mb.group(1) if mb else ""

    # price
    mp = re.search(r"(\d[\d\s]{3,})(?:\s*(?:baht|бат|฿|b|thb))?", t)
    price = re.sub(r"\s", "", mp.group(1)) if mp else ""

    pets_allowed = "unknown"
    if "без питомц" in t or "no pets" in t:
        pets_allowed = "no"
    elif "с питомц" in t or "pets ok" in t or "pet friendly" in t:
        pets_allowed = "yes"

    pool = "yes" if ("pool" in t or "бассейн" in t) else "no"
    furnished = "yes" if ("furnished" in t or "мебел" in t) else "unknown"

    mt = re.search(r"^([^\n]{10,80})", text.strip())
    title = mt.group(1) if mt else ""

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
    return row

# ---------- Подбор из памяти ----------
def suggest_listings(area: str, bedrooms: int, budget: int) -> List[Dict[str, Any]]:
    area_l = (area or "").lower()
    res = []
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

# ---------- GPT small talk ----------
def gpt_reply(text: str, system_hint: str = "") -> Optional[str]:
    if not OPENAI_API_KEY:
        return None
    try:
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": GPT_MODEL,
            "messages": [
                {"role": "system", "content": system_hint or "Ты дружелюбный русскоязычный ассистент агенства недвижимости на Самуи. Отвечай кратко и по делу."},
                {"role": "user", "content": text},
            ]
        }
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning("GPT error: %s", e)
        return None

# ---------- Диалог /rent ----------
(
    AREA, BEDROOMS, BUDGET, PEOPLE, PETS, POOL, WORKSPACE, PHONE, NAME, NOTES
) = range(10)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Покажем приветствие при /start (после нажатия пользователем кнопки "Старт")
    await update.message.reply_text(GREETING_MESSAGE)

async def rent_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🗺️ В каком районе Самуи хотите жить? (Маенам, Бопхут, Чавенг, Ламай ...)")
    return AREA

async def rent_area(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["location"] = update.message.text.strip()
    await update.message.reply_text("🛏️ Сколько спален нужно?")
    return BEDROOMS

async def rent_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = re.search(r"\d+", update.message.text)
    context.user_data["bedrooms"] = int(m.group()) if m else 1
    await update.message.reply_text("💰 Какой бюджет в месяц (бат)?")
    return BUDGET

async def rent_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = re.search(r"\d+", update.message.text.replace(" ", ""))
    context.user_data["budget"] = int(m.group()) if m else 0
    await update.message.reply_text("👨‍👩‍👧‍👦 Сколько человек будет проживать?")
    return PEOPLE

async def rent_people(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = re.search(r"\d+", update.message.text)
    context.user_data["people"] = int(m.group()) if m else 1
    await update.message.reply_text("🐾 С питомцами? (да/нет)")
    return PETS

async def rent_pets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.lower()
    context.user_data["pets"] = "yes" if "да" in t or "yes" in t else ("no" if "нет" in t or "no" in t else "unknown")
    await update.message.reply_text("🏊 Нужен бассейн? (да/нет)")
    return POOL

async def rent_pool(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.lower()
    context.user_data["pool"] = "yes" if "да" in t or "yes" in t else ("no" if "нет" in t or "no" in t else "unknown")
    await update.message.reply_text("💻 Нужна рабочая зона/стол? (да/нет)")
    return WORKSPACE

async def rent_workspace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.lower()
    context.user_data["workspace"] = "yes" if "да" in t or "yes" in t else ("no" if "нет" in t or "no" in t else "unknown")
    await update.message.reply_text("📞 Укажите телефон (или напишите 'нет'):")
    return PHONE

async def rent_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["phone"] = update.message.text.strip()
    await update.message.reply_text("👤 Как к вам обращаться?")
    return NAME

async def rent_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("📝 Есть особые пожелания? (кратко)")
    return NOTES

async def rent_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["notes"] = update.message.text.strip()

    # Сохраняем лид
    u = update.effective_user
    lead = {
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        "chat_id": str(update.effective_chat.id),
        "username": (u.username or u.full_name or "").strip(),
        "location": context.user_data.get("location", ""),
        "bedrooms": str(context.user_data.get("bedrooms", "")),
        "budget": str(context.user_data.get("budget", "")),
        "people": str(context.user_data.get("people", "")),
        "pets": context.user_data.get("pets", ""),
        "pool": context.user_data.get("pool", ""),
        "workspace": context.user_data.get("workspace", ""),
        "phone": context.user_data.get("phone", ""),
        "name": context.user_data.get("name", ""),
        "notes": context.user_data.get("notes", ""),
    }
    leads_append(lead)

    # Сообщим менеджеру
    if MANAGER_CHAT_ID:
        try:
            msg = (
                "Новый лид:\n"
                f"Имя: {lead['name']}\nТел: {lead['phone']}\nЮзер: @{u.username or '-'}\n"
                f"Район: {lead['location']} | Спальни: {lead['bedrooms']} | Бюджет: {lead['budget']}\n"
                f"Люди: {lead['people']} | Питомцы: {lead['pets']} | Бассейн: {lead['pool']} | Раб.место: {lead['workspace']}\n"
                f"Пожелания: {lead['notes']}"
            )
            await context.bot.send_message(chat_id=int(MANAGER_CHAT_ID), text=msg)
        except Exception as e:
            log.warning("Не удалось отправить менеджеру: %s", e)

    # Подберём из памяти
    area = lead["location"]
    try:
        bedrooms = int(lead["bedrooms"] or 0)
    except Exception:
        bedrooms = 0
    try:
        budget = int(lead["budget"] or 0)
    except Exception:
        budget = 0

    offers = suggest_listings(area, bedrooms, budget)
    if offers:
        lines = ["Нашёл подходящие варианты:"]
        for o in offers:
            lines.append(
                f"• {o.get('title') or 'Лот'} — спальни: {o.get('bedrooms') or '?'}; "
                f"цена: {o.get('price_month') or '?'}; район: {o.get('location') or '?'}\n{o.get('link') or ''}"
            )
        await update.message.reply_text("\n\n".join(lines))
    else:
        await update.message.reply_text("Спасибо! 🙌 Я передал заявку менеджеру. Мы скоро свяжемся.")

    await update.message.reply_text("Если захотите, можно начать заново: /rent")
    return ConversationHandler.END

async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Опрос отменён. Введите /rent чтобы начать заново.")
    return ConversationHandler.END

# ---------- Обработка постов канала ----------
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
    item = parse_listing_from_text(text, link, listing_id)
    IN_MEMORY_LISTINGS.append(item)
    listings_append(item)
    log.info("Сохранён лот %s из канала @%s", listing_id, msg.chat.username)

# ---------- Свободный чат с GPT ----------
async def smalltalk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    reply = gpt_reply(text)
    if reply:
        await update.message.reply_text(reply)
    else:
        await update.message.reply_text("Я вас слышу. Можете также запустить анкету по аренде: /rent 😊")

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
            PEOPLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_people)],
            PETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_pets)],
            POOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_pool)],
            WORKSPACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_workspace)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_phone)],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_name)],
            NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_notes)],
        },
        fallbacks=[CommandHandler("cancel", rent_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Посты канала
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, on_channel_post))

    # Small talk: приватный чат, любые тексты не-команды
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, smalltalk))

    return app

# ---------- Запуск (БЕЗ await!) ----------
def run_webhook_blocking(app: Application) -> None:
    webhook_url = f"{BASE_URL}{WEBHOOK_PATH if WEBHOOK_PATH.startswith('/') else '/'+WEBHOOK_PATH}"
    log.info("Webhook URL: %s", webhook_url)
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        url_path=WEBHOOK_PATH.lstrip("/"),
        webhook_url=webhook_url,
        allowed_updates=["message", "channel_post", "callback_query"],
        drop_pending_updates=True,
    )

def run_polling_blocking(app: Application) -> None:
    app.run_polling(
        allowed_updates=["message", "channel_post", "callback_query"],
        drop_pending_updates=True,
    )

def main() -> None:
    setup_gsheets_if_possible()
    app = build_application()
    if BASE_URL:
        log.info("Стартуем в режиме WEBHOOK…")
        run_webhook_blocking(app)
    else:
        log.info("Стартуем в режиме POLLING…")
        run_polling_blocking(app)

if __name__ == "__main__":
    main()
