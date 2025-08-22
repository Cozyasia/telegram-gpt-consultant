# main.py
import os
import re
import json
import time
import logging
import asyncio
from typing import Iterable, List, Dict, Optional, Union

import requests
import gspread
from google.oauth2.service_account import Credentials

from telegram import Update
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("cozyasia-bot")

# ---------- ENV ----------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("Env var TELEGRAM_BOT_TOKEN is missing")

# Webhook / Polling
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "cozy-secret").strip()
PORT             = int(os.getenv("PORT", "10000"))

# OpenAI (опционально)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "30"))

# Google Sheets
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SHEETS_DB_ID = os.environ.get("GOOGLE_SHEETS_DB_ID", "").strip()
LEADS_TAB = os.getenv("LEADS_TAB", "Leads")
LISTINGS_TAB = os.getenv("LISTINGS_TAB", "Listings")

# Telegram channel / admin
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()  # без '@'
MANAGER_CHAT_ID_RAW = os.getenv("MANAGER_CHAT_ID", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

def _parse_chat_id(s: str) -> Union[int, None]:
    if not s:
        return None
    if s.lstrip("-").isdigit():
        return int(s)
    return None

MANAGER_CHAT_ID = _parse_chat_id(MANAGER_CHAT_ID_RAW)

# Приветствие из ENV (можно менять без редеплоя кода)
WELCOME_TEXT = os.getenv(
    "WELCOME_TEXT",
    "✅ Я уже тут!\n"
    "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n"
    "👉 Или нажмите команду /rent — я задам несколько вопросов о жилье, сформирую заявку, предложу варианты и передам менеджеру. "
    "Он свяжется с вами для уточнения деталей и бронирования."
)

SYSTEM_PROMPT = (
    "Ты — Cozy Asia Consultant, дружелюбный и чёткий помощник по аренде/покупке недвижимости на Самуи. "
    "Отвечай кратко и по делу; если сведений не хватает — задай 1 уточняющий вопрос. "
    "Не выдумывай; приоритет — предлагать варианты из внутренней базы (таблица Listings)."
)

# ---------- Google Sheets ----------
LISTING_HEADERS = [
    "id","title","area","bedrooms","price_thb","distance_to_sea_m",
    "pets","available_from","available_to","link","message_id","status","notes"
]
LEAD_HEADERS = [
    "ts","source","name","phone","area","bedrooms","guests","pets","budget_thb",
    "check_in","check_out","transfer","requirements","listing_id","telegram_user_id","username"
]

def gs_client():
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEETS_DB_ID:
        raise RuntimeError("Google Sheets env vars are missing")
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds)

def ws_get_or_create(sh, name: str, headers: List[str]):
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(name, rows=2000, cols=40)
        ws.append_row(headers)
    return ws

def get_ws(name: str, headers: List[str]):
    client = gs_client()
    sh = client.open_by_key(GOOGLE_SHEETS_DB_ID)
    return ws_get_or_create(sh, name, headers)

def listings_all() -> List[Dict]:
    ws = get_ws(LISTINGS_TAB, LISTING_HEADERS)
    return ws.get_all_records()

def listings_append_if_new(item: Dict):
    """Добавляет запись в Listings, если message_id ещё не встречался."""
    ws = get_ws(LISTINGS_TAB, LISTING_HEADERS)
    rows = ws.get_all_records()
    mid = str(item.get("message_id", "")).strip()
    if mid and any(str(r.get("message_id","")) == mid for r in rows):
        return
    ws.append_row([item.get(h, "") for h in LISTING_HEADERS])

def leads_append(row: List):
    ws = get_ws(LEADS_TAB, LEAD_HEADERS)
    ws.append_row(row)

# ---------- Utils ----------
def is_admin(user_id: Optional[int]) -> bool:
    return (not ADMIN_IDS) or (user_id in ADMIN_IDS)

def chunk_text(text: str, limit: int = 4096) -> Iterable[str]:
    for i in range(0, len(text), limit):
        yield text[i:i+limit]

def to_int(s: str, default: int = 0) -> int:
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else default

def yes_no(s: str) -> Optional[bool]:
    t = (s or "").strip().lower()
    if t in ["да","yes","y","ага","true","1","нужно"]:
        return True
    if t in ["нет","no","n","false","0","не","не нужно","не надо"]:
        return False
    return None

def listing_link(item: Dict) -> str:
    if item.get("link"):
        return str(item["link"])
    mid = str(item.get("message_id") or "").strip()
    if CHANNEL_USERNAME and mid:
        return f"https://t.me/{CHANNEL_USERNAME}/{mid}"
    return ""

def format_listing(item: Dict) -> str:
    parts = [
        f"<b>{item.get('title','Без названия')}</b>",
        f"Район: {item.get('area','?')}",
        f"Спален: {item.get('bedrooms','?')} | Цена: {item.get('price_thb','?')} ฿/мес",
    ]
    if item.get("distance_to_sea_m"):
        parts.append(f"До моря: {item['distance_to_sea_m']} м")
    if str(item.get("pets","")).strip():
        parts.append(f"Питомцы: {item['pets']}")
    link = listing_link(item)
    if link:
        parts.append(f"\n<a href=\"{link}\">Открыть объявление</a>")
    return "\n".join(parts)

# ---------- Алиасы районов и нормализация ----------
AREA_ALIASES = {
    "lamai":   ["lamai", "ламай"],
    "maenam":  ["maenam", "маенам"],
    "bophut":  ["bophut", "бопхут", "бопут"],
    "chaweng": ["chaweng", "чавенг"],
    "bangrak": ["bangrak", "банграк", "банкрак"],
    "choengmon": ["choengmon","чоэнг мон","чоенгмон","чонгмон"],
}
def norm_area(s: str) -> str:
    t = (s or "").strip().lower()
    for canon, variants in AREA_ALIASES.items():
        if any(v in t for v in variants):
            return canon
    return t

# ---------- Поиск с «мягкими» условиями ----------
def search_listings(area: str = "", bedrooms: int = 0, budget_thb: int = 0, pets: Optional[bool] = None) -> List[Dict]:
    items = listings_all()
    out = []
    area_n = norm_area(area)
    for it in items:
        if str(it.get("status","")).lower() in ["sold","inactive","hidden","закрыто","продано","сдано"]:
            continue
        if area_n:
            it_area_n = norm_area(str(it.get("area","")))
            if area_n not in it_area_n:
                continue
        if bedrooms and to_int(str(it.get("bedrooms","0"))) < bedrooms:
            continue
        if budget_thb and to_int(str(it.get("price_thb","0"))) > budget_thb:
            continue
        if pets is True:
            val = str(it.get("pets","")).strip().lower()
            # отбрасываем только явные запреты, пустое считаем «возможно»
            if val in ["нет","no","false","запрещены","not allowed","no pets"]:
                continue
        out.append(it)

    out.sort(key=lambda x: to_int(str(x.get("price_thb","0"))))
    # fallback: если ничего не нашли — ослабим район
    if not out and area_n:
        return search_listings(area="", bedrooms=bedrooms, budget_thb=budget_thb, pets=pets)
    return out[:3]

# ---------- Парсер постов канала ----------
LOT_RE   = re.compile(r"(?:Лот|Lot)[^\d]*(\d+)", re.I)
AREA_RE  = re.compile(r"(?:Район|Area)\s*[:\-]\s*([^\n]+)", re.I)
BEDS_RE  = re.compile(r"(?:спален|спальн|bedrooms?)\s*[:\-]?\s*(\d+)", re.I)
PRICE_RE = re.compile(r"(?:цена|price)\s*[:\-]?\s*([\d\s]+)", re.I)

def parse_listing_text(text: str) -> Dict:
    t = text or ""
    lot = LOT_RE.search(t)
    area = AREA_RE.search(t)
    beds = BEDS_RE.search(t)
    price = PRICE_RE.search(t)
    return {
        "id": (lot.group(1) if lot else ""),
        "area": (area.group(1).strip() if area else ""),
        "bedrooms": to_int(beds.group(1) if beds else ""),
        "price_thb": to_int(price.group(1) if price else ""),
        "title": t.splitlines()[0][:120] if t else "Без названия",
        "status": "active",
    }

# ---------- OpenAI (не обязательно) ----------
def _chat_completion(messages: List[Dict]) -> str:
    if not OPENAI_API_KEY:
        return "Готов помочь с жильём и островом. Напишите /rent — подберу варианты из базы."
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": OPENAI_MODEL, "messages": messages, "temperature": 0.3, "max_tokens": 500}
    r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=OPENAI_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()

async def ai_answer(prompt: str) -> str:
    msgs = [{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":prompt}]
    return await asyncio.to_thread(_chat_completion, msgs)

# ---------- States ----------
(ASK_AREA, ASK_BEDROOMS, ASK_GUESTS, ASK_PETS, ASK_BUDGET,
 ASK_CHECKIN, ASK_CHECKOUT, ASK_TRANSFER, ASK_NAME, ASK_PHONE, ASK_REQS, DONE) = range(12)

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT)

# ---- /rent ----
async def rent_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Начнём. Какой район Самуи предпочитаете? (например: Маенам, Бопхут, Чавенг, Ламай)")
    return ASK_AREA

async def ask_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["area"] = update.message.text.strip()
    await update.message.reply_text("Сколько спален нужно?")
    return ASK_BEDROOMS

async def ask_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bedrooms"] = to_int(update.message.text, 1)
    await update.message.reply_text("Сколько гостей будет проживать?")
    return ASK_GUESTS

async def ask_guests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["guests"] = to_int(update.message.text, 1)
    await update.message.reply_text("Питомцы будут? (да/нет)")
    return ASK_PETS

async def ask_pets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    yn = yes_no(update.message.text)
    context.user_data["pets"] = yn if yn is not None else False
    await update.message.reply_text("Какой бюджет на месяц (в батах)?")
    return ASK_BUDGET

async def ask_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["budget_thb"] = to_int(update.message.text, 0)
    await update.message.reply_text("Дата заезда (например: 2025-09-01)?")
    return ASK_CHECKIN

async def ask_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["check_in"] = update.message.text.strip()
    await update.message.reply_text("Дата выезда (например: 2026-03-01)?")
    return ASK_CHECKOUT

async def ask_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["check_out"] = update.message.text.strip()
    await update.message.reply_text("Нужен ли трансфер из аэропорта? (да/нет)")
    return ASK_TRANSFER

async def ask_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["transfer"] = yes_no(update.message.text) is True
    await update.message.reply_text("Ваше имя и фамилия?")
    return ASK_NAME

async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("Контактный телефон (включая код страны)?")
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["phone"] = update.message.text.strip()
    await update.message.reply_text("Есть ли дополнительные требования? (вид на море, бассейн, рабочее место и т.д.)")
    return ASK_REQS

async def finish_lead(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["requirements"] = update.message.text.strip()
    area = context.user_data.get("area","")
    bedrooms = int(context.user_data.get("bedrooms",1))
    budget = int(context.user_data.get("budget_thb",0))
    pets = context.user_data.get("pets", False)

    try:
        matches = search_listings(area=area, bedrooms=bedrooms, budget_thb=budget, pets=pets)
    except Exception as e:
        log.exception("Search listings error: %s", e)
        matches = []

    if matches:
        await update.message.reply_text("Подобрал варианты из нашей базы (первые 3):")
        for it in matches:
            for chunk in chunk_text(format_listing(it)):
                await update.message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
    else:
        await update.message.reply_text("Пока точных совпадений не нашёл. Я передам менеджеру ваш запрос — он предложит индивидуальные варианты в течение дня.")

    u = update.effective_user
    lead = context.user_data.copy()
    lead["listing_id"] = context.user_data.get("listing_id","")
    lead_row = [
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "bot",
        lead.get("name",""),
        lead.get("phone",""),
        lead.get("area",""),
        lead.get("bedrooms",""),
        lead.get("guests",""),
        "да" if lead.get("pets") else "нет",
        lead.get("budget_thb",""),
        lead.get("check_in",""),
        lead.get("check_out",""),
        "да" if lead.get("transfer") else "нет",
        lead.get("requirements",""),
        lead.get("listing_id",""),
        str(u.id if u else ""),
        u.username if (u and u.username) else "",
    ]
    try:
        leads_append(lead_row)
    except Exception as e:
        log.exception("Ошибка записи лида в Google Sheets: %s", e)

    if MANAGER_CHAT_ID:
        try:
            text = (
                "<b>Новая заявка</b>\n"
                f"Имя: {lead.get('name')}\nТел: {lead.get('phone')}\n"
                f"Район: {lead.get('area')} | Спален: {lead.get('bedrooms')} | Гостей: {lead.get('guests')}\n"
                f"Питомцы: {'да' if lead.get('pets') else 'нет'} | Бюджет: {lead.get('budget_thb')} ฿\n"
                f"Даты: {lead.get('check_in')} → {lead.get('check_out')} | Трансфер: {'да' if lead.get('transfer') else 'нет'}\n"
                f"Пожелания: {lead.get('requirements')}\n"
                f"Listing ID: {lead.get('listing_id')}\n"
                f"TG: @{u.username if (u and u.username) else u.id}"
            )
            await context.bot.send_message(chat_id=MANAGER_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
        except Exception:
            log.exception("Не удалось отправить уведомление менеджеру")

    await update.message.reply_text("Спасибо! Заявка зафиксирована. Менеджер Cozy Asia свяжется с вами.")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Окей, отменил.")
    return ConversationHandler.END

# ---- AI fallback ----
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    prompt = update.message.text.strip()
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    if re.search(r"(help|подобрать|найти|дом|вил+а|квартира|аренда)", prompt.lower()):
        await update.message.reply_text("Могу запустить быстрый опрос и предложить варианты из нашей базы. Напишите /rent.")
    if not OPENAI_API_KEY:
        return
    try:
        reply = await ai_answer(prompt)
        for chunk in chunk_text(reply):
            await update.message.reply_text(chunk)
    except Exception as e:
        log.exception("OpenAI error: %s", e)

# ---- Приём постов из канала: добавление в Listings ----
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return
    text = msg.text or msg.caption or ""
    item = parse_listing_text(text)
    item["message_id"] = msg.message_id
    if CHANNEL_USERNAME:
        item["link"] = f"https://t.me/{CHANNEL_USERNAME}/{msg.message_id}"
    try:
        listings_append_if_new(item)
        log.info("Saved channel post %s to Listings", msg.message_id)
    except Exception as e:
        log.exception("Error writing listing: %s", e)

# ---- Post to channel (админы, опционально) ----
async def post_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Функция отключена в этой сборке.")
    # Можно включить при необходимости: отправка в канал по /post

# ---------- ENTRY ----------
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # /start
    app.add_handler(CommandHandler("start", start))

    # /rent conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_entry)],
        states={
            ASK_AREA:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_area)],
            ASK_BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_bedrooms)],
            ASK_GUESTS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_guests)],
            ASK_PETS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_pets)],
            ASK_BUDGET:   [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_budget)],
            ASK_CHECKIN:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_checkin)],
            ASK_CHECKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_checkout)],
            ASK_TRANSFER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_transfer)],
            ASK_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_REQS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_lead)],
        },
        fallbacks=[CommandHandler("cancel", cancel_wizard)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Приём постов из канала
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))

    # AI-ответы по умолчанию
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # --- запуск ---
    allowed = ["message", "channel_post", "edited_channel_post"]

    if WEBHOOK_BASE_URL:
        url_path = f"telegram/{WEBHOOK_SECRET}"
        full_url = f"{WEBHOOK_BASE_URL}/{url_path}"
        log.info("Starting WEBHOOK on 0.0.0.0:%s, url=%s", PORT, full_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=url_path,
            webhook_url=full_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=allowed,
        )
    else:
        log.info("Starting POLLING…")
        app.run_polling(allowed_updates=allowed, drop_pending_updates=True)

if __name__ == "__main__":
    main()
