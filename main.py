import os
import re
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
)

# ---------------------- ЛОГИ ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cozy_asia_bot")

# ---------------------- ENV ----------------------
def _env(key: str, default: Optional[str] = None) -> str:
    v = os.environ.get(key, default if default is not None else "")
    return v.strip() if isinstance(v, str) else v

TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")

GREETING_TEXT = _env(
    "GREETING_TEXT",
    "✅ Я уже тут!\n🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n\n"
    "👉 Или нажмите команду /rent — задам несколько вопросов о жилье, сформирую заявку, предложу варианты и передам менеджеру."
)

# Канал без @ и без https://t.me/
PUBLIC_CHANNEL_USERNAME = _env("PUBLIC_CHANNEL_USERNAME")
# Менеджер (опционально): кому слать новые лиды
MANAGER_CHAT_ID = _env("MANAGER_CHAT_ID", "")

# Google Sheets
GOOGLE_SHEETS_DB_ID = _env("GOOGLE_SHEETS_DB_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = _env("GOOGLE_SERVICE_ACCOUNT_JSON")
if not GOOGLE_SHEETS_DB_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
    raise RuntimeError("GOOGLE_SHEETS_DB_ID or GOOGLE_SERVICE_ACCOUNT_JSON is empty")

# Вебхук/порт (если BASE_URL пуст, бот уйдёт в polling)
BASE_URL = _env("BASE_URL").rstrip("/")
WEBHOOK_PATH = _env("WEBHOOK_PATH", f"/{TELEGRAM_BOT_TOKEN}")
PORT = int(_env("PORT", "10000"))

# ---------------------- CONSTANTS ----------------------
LISTINGS_SHEET = "Listings"
LEADS_SHEET = "Leads"

LISTINGS_HEADERS = [
    "listing_id", "created_at", "title", "description", "location",
    "bedrooms", "bathrooms", "price_month", "pets_allowed", "utilities",
    "electricity_rate", "water_rate", "area_m2", "pool", "furnished",
    "link", "images", "tags", "raw_text",
]

LEADS_HEADERS = [
    "created_at", "chat_id", "username", "location", "budget",
    "bedrooms", "pets", "dates", "matched_ids"
]

# кэш лотов
LISTINGS_CACHE: List[Dict[str, Any]] = []

# ---------------------- GOOGLE SHEETS ----------------------
def _gc_client():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)

def ensure_worksheet(sh, title: str, headers: List[str]):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=max(50, len(headers)))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        return ws

    # Обновим заголовки, если пусто
    try:
        first_row = ws.row_values(1)
        if [h.strip() for h in first_row] != headers:
            ws.update("1:1", [headers])
    except Exception as e:
        log.warning("Header check failed for %s: %s", title, e)
    return ws

def gsheets_open():
    gc = _gc_client()
    sh = gc.open_by_key(GOOGLE_SHEETS_DB_ID)
    ensure_worksheet(sh, LISTINGS_SHEET, LISTINGS_HEADERS)
    ensure_worksheet(sh, LEADS_SHEET, LEADS_HEADERS)
    return sh

def listings_refresh_cache(sh=None):
    global LISTINGS_CACHE
    if sh is None:
        sh = gsheets_open()
    ws = sh.worksheet(LISTINGS_SHEET)
    values = ws.get_all_records()
    # нормализуем поля
    cache = []
    for row in values:
        try:
            item = {k: (str(v).strip() if v is not None else "") for k, v in row.items()}
            # числа
            item["price_month"] = _to_int(item.get("price_month"))
            item["bedrooms"] = _to_int(item.get("bedrooms"))
            item["bathrooms"] = _to_int(item.get("bathrooms"))
            item["area_m2"] = _to_int(item.get("area_m2"))
            item["pool"] = _to_bool(item.get("pool"))
            item["pets_allowed"] = _to_bool(item.get("pets_allowed"))
            # location канонизируем
            item["location"] = canon_location(item.get("location"))
            cache.append(item)
        except Exception as e:
            log.warning("Bad row in Listings skipped: %s", e)
    LISTINGS_CACHE = cache
    log.info("Listings cache loaded: %d items", len(LISTINGS_CACHE))

def append_listing(row: Dict[str, Any], sh=None):
    if sh is None:
        sh = gsheets_open()
    ws = sh.worksheet(LISTINGS_SHEET)
    ordered = [row.get(h, "") for h in LISTINGS_HEADERS]
    ws.append_row(ordered, value_input_option="USER_ENTERED")

def append_lead(row: Dict[str, Any], sh=None):
    if sh is None:
        sh = gsheets_open()
    ws = sh.worksheet(LEADS_SHEET)
    ordered = [row.get(h, "") for h in LEADS_HEADERS]
    ws.append_row(ordered, value_input_option="USER_ENTERED")

# ---------------------- HELPERS ----------------------
def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    s = str(v).strip().replace(" ", "")
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None

def _to_bool(v) -> Optional[bool]:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("да", "true", "yes", "y", "1"):
        return True
    if s in ("нет", "false", "no", "n", "0"):
        return False
    return None

LOC_ALIASES = {
    "lamai": "lamai", "ламай": "lamai",
    "bophut": "bophut", "бопхут": "bophut",
    "chaweng": "chaweng", "чавенг": "chaweng",
    "maenam": "maenam", "маенам": "maenam",
    "bangrak": "bangrak", "банграк": "bangrak",
    "lipanoi": "lipanoi", "липаной": "lipanoi", "lipa noi": "lipanoi", "липа ной": "lipanoi",
}

def canon_location(s: str) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    for k, v in LOC_ALIASES.items():
        if k in s:
            return v
    # оставить первое слово
    return s.split()[0]

def parse_price(text: str) -> Optional[int]:
    # ищем 5+ цифр (цена в батах)
    t = (text or "").replace("\u00a0", " ")
    m = re.search(r"(?:฿|бат|baht|thb)?\s*([\d\s]{4,})", t, flags=re.I)
    if m:
        return _to_int(m.group(1))
    return None

def parse_bedrooms(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*(спальн|bed)", text, flags=re.I)
    return int(m.group(1)) if m else None

def parse_bathrooms(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*(с/у|ванн|bath)", text, flags=re.I)
    return int(m.group(1)) if m else None

def parse_area(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*(м2|m2|sqm|кв\.?\s*м)", text, flags=re.I)
    return int(m.group(1)) if m else None

def parse_location(text: str) -> str:
    return canon_location(text)

def parse_pets(text: str) -> Optional[bool]:
    if re.search(r"питомц|pets|animals", text, flags=re.I):
        if re.search(r"без\s+питомц|no\s+pets", text, flags=re.I):
            return False
        return True
    return None

def parse_pool(text: str) -> Optional[bool]:
    if re.search(r"бассейн|pool", text, flags=re.I):
        return True
    return None

def pick_urls(text: str) -> List[str]:
    return re.findall(r"https?://\S+", text or "")

def tme_link(username: str, message_id: int) -> str:
    return f"https://t.me/{username}/{message_id}"

def match_listings(criteria: Dict[str, Any]) -> List[Dict[str, Any]]:
    loc = canon_location(criteria.get("location", ""))
    budget = _to_int(criteria.get("budget"))
    beds = _to_int(criteria.get("bedrooms"))
    pets = criteria.get("pets")
    results = []
    for it in LISTINGS_CACHE:
        if loc and it.get("location") and it["location"] != loc:
            continue
        if beds and it.get("bedrooms") and it["bedrooms"] != beds:
            # можно ослабить: допускаем >=?
            # continue
            pass
        if budget and it.get("price_month") and it["price_month"] and it["price_month"] > budget:
            continue
        if isinstance(pets, bool) and it.get("pets_allowed") is not None:
            if it["pets_allowed"] != pets:
                continue
        results.append(it)
    # сортируем по цене
    results.sort(key=lambda x: x.get("price_month") or 10**9)
    return results[:5]

def fmt_listing(it: Dict[str, Any]) -> str:
    parts = []
    if it.get("title"):
        parts.append(f"🏷 {it['title']}")
    if it.get("location"):
        parts.append(f"📍 {it['location'].title()}")
    badges = []
    if it.get("bedrooms"):
        badges.append(f"{it['bedrooms']} сп.")
    if it.get("bathrooms"):
        badges.append(f"{it['bathrooms']} с/у")
    if it.get("area_m2"):
        badges.append(f"{it['area_m2']} м²")
    if it.get("pool"):
        badges.append("бассейн")
    if badges:
        parts.append(" · ".join(badges))
    if it.get("price_month"):
        parts.append(f"💰 {it['price_month']:,} ฿/мес".replace(",", " "))
    if it.get("link"):
        parts.append(f"🔗 {it['link']}")
    elif it.get("images"):
        parts.append(f"🖼 {it['images']}")
    return "\n".join(parts)

# ---------------------- HANDLERS ----------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(GREETING_TEXT)

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(f"Ваш chat_id: `{update.effective_chat.id}`", parse_mode="Markdown")

# --- Анкета /rent ---
(ASK_LOCATION, ASK_BUDGET, ASK_BEDROOMS, ASK_PETS, ASK_DATES) = range(5)

async def rent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_chat.send_message("Начнём. Какой район Самуи предпочитаете? (например: Маенам, Бопхут, Чавенг, Ламай)")
    return ASK_LOCATION

async def rent_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = canon_location(update.message.text)
    context.user_data["location"] = loc
    await update.effective_chat.send_message("Какой бюджет в месяц (в батах)?")
    return ASK_BUDGET

async def rent_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    budget = _to_int(update.message.text)
    if not budget:
        await update.effective_chat.send_message("Введите число, например 50000")
        return ASK_BUDGET
    context.user_data["budget"] = budget
    await update.effective_chat.send_message("Сколько спален нужно?")
    return ASK_BEDROOMS

async def rent_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    beds = _to_int(update.message.text)
    if not beds:
        await update.effective_chat.send_message("Введите число, например 2")
        return ASK_BEDROOMS
    context.user_data["bedrooms"] = beds
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("С питомцами", callback_data="pets_yes"),
         InlineKeyboardButton("Без питомцев", callback_data="pets_no")],
    ])
    await update.effective_chat.send_message("С питомцами?", reply_markup=kb)
    return ASK_PETS

async def rent_pets_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pets = True if query.data == "pets_yes" else False
    context.user_data["pets"] = pets
    await query.edit_message_text("Уточните, пожалуйста, планируемые даты (можно пропустить).")
    return ASK_DATES

async def rent_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dates = update.message.text.strip()
    context.user_data["dates"] = dates

    # Подбор
    criteria = context.user_data.copy()
    results = match_listings(criteria)
    if results:
        await update.effective_chat.send_message("Нашёл подходящее:")
        for it in results:
            await update.effective_chat.send_message(fmt_listing(it))
    else:
        await update.effective_chat.send_message("Пока ничего не нашёл в базе, передам заявку менеджеру.")

    # Сохраним лид
    try:
        sh = gsheets_open()
        append_lead({
            "created_at": datetime.utcnow().isoformat(),
            "chat_id": str(update.effective_chat.id),
            "username": update.effective_user.username or "",
            "location": criteria.get("location", ""),
            "budget": str(criteria.get("budget", "")),
            "bedrooms": str(criteria.get("bedrooms", "")),
            "pets": "да" if criteria.get("pets") else "нет",
            "dates": criteria.get("dates", ""),
            "matched_ids": ",".join([str(x.get("listing_id", "")) for x in results]) if results else "",
        }, sh=sh)
    except Exception as e:
        log.warning("Append lead error: %s", e)

    # Уведомим менеджера
    try:
        if MANAGER_CHAT_ID:
            txt = (f"Новый лид:\n"
                   f"📍 {criteria.get('location','')}\n"
                   f"💰 {criteria.get('budget','')} ฿\n"
                   f"🛏 {criteria.get('bedrooms','')}\n"
                   f"🐾 {'да' if criteria.get('pets') else 'нет'}\n"
                   f"📅 {criteria.get('dates','')}\n"
                   f"from @{update.effective_user.username or 'user'} / {update.effective_chat.id}")
            await context.bot.send_message(int(MANAGER_CHAT_ID), txt)
    except Exception as e:
        log.warning("Notify manager error: %s", e)

    context.user_data.clear()
    return ConversationHandler.END

async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_chat.send_message("Ок, отменил. Напишите /rent когда будете готовы.")
    return ConversationHandler.END

# --- Быстрый свободный запрос (например: "ламай 2 спальни до 50000") ---
async def free_text_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    crit = {
        "location": canon_location(text),
        "budget": parse_price(text) or _to_int(text),
        "bedrooms": parse_bedrooms(text) or _to_int(text),
        "pets": True if re.search(r"с\s*питомц|pets", text, flags=re.I) else None,
    }
    results = match_listings(crit)
    if results:
        await update.effective_chat.send_message("Похоже, вам подойдут эти варианты:")
        for it in results:
            await update.effective_chat.send_message(fmt_listing(it))
    else:
        await update.effective_chat.send_message("Пока ничего не нашёл в базе. Запустить анкету? /rent")

# --- Приём постов из канала ---
async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post:
        return
    # проверим что это нужный канал
    try:
        uname = (post.chat.username or "").lower()
        if PUBLIC_CHANNEL_USERNAME and uname != PUBLIC_CHANNEL_USERNAME.lower():
            return
    except Exception:
        return

    text = (post.text or post.caption or "").strip()
    if not text:
        return

    # распарсим
    title = ""
    description = text
    location = canon_location(text)
    price = parse_price(text)
    bedrooms = parse_bedrooms(text)
    bathrooms = parse_bathrooms(text)
    area = parse_area(text)
    pets = parse_pets(text)
    pool = parse_pool(text)
    urls = pick_urls(text)

    listing_id = str(post.message_id)
    link = tme_link(PUBLIC_CHANNEL_USERNAME, post.message_id)

    row = {
        "listing_id": listing_id,
        "created_at": datetime.utcnow().isoformat(),
        "title": title,
        "description": description,
        "location": location,
        "bedrooms": bedrooms or "",
        "bathrooms": bathrooms or "",
        "price_month": price or "",
        "pets_allowed": "да" if pets else ("нет" if pets is False else ""),
        "utilities": "",
        "electricity_rate": "",
        "water_rate": "",
        "area_m2": area or "",
        "pool": "да" if pool else ("нет" if pool is False else ""),
        "furnished": "",
        "link": link,
        "images": ",".join(urls),
        "tags": "",
        "raw_text": text,
    }

    try:
        sh = gsheets_open()
        append_listing(row, sh=sh)
        listings_refresh_cache(sh=sh)
        log.info("Saved listing %s to Sheets", listing_id)
    except Exception as e:
        log.error("Failed to save listing: %s", e)

# ---------------------- APP BUILD ----------------------
def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("id", id_cmd))

    # Анкета /rent
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_cmd)],
        states={
            ASK_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_location)],
            ASK_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_budget)],
            ASK_BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_bedrooms)],
            ASK_PETS: [CallbackQueryHandler(rent_pets_cb, pattern=r"^pets_(yes|no)$")],
            ASK_DATES: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_dates)],
        },
        fallbacks=[CommandHandler("cancel", rent_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Свободные тексты в личке
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, free_text_query))

    # Посты из канала
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_post_handler))

    return app

# ---------------------- MAIN ----------------------
def main():
    # подготовим кэш
    try:
        listings_refresh_cache()
    except Exception as e:
        log.warning("Cache init failed: %s", e)

    app = build_app()

    # На всякий — уберём старый вебхук и висячие апдейты
    try:
        import asyncio
        asyncio.run(app.bot.delete_webhook(drop_pending_updates=True))
    except Exception:
        pass

    if BASE_URL:
        log.info("Starting WEBHOOK on %s%s", BASE_URL, WEBHOOK_PATH)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH.lstrip("/"),
            webhook_url=f"{BASE_URL}{WEBHOOK_PATH}",
            drop_pending_updates=True,
            allowed_updates=("message", "channel_post", "callback_query"),
        )
    else:
        log.info("BASE_URL not set -> starting POLLING")
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=("message", "channel_post", "callback_query"),
        )

if __name__ == "__main__":
    main()
