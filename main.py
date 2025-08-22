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
    ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("cozyasia-bot")

# ---------- ENV ----------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("Env var TELEGRAM_BOT_TOKEN is missing")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "30"))

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SHEETS_DB_ID = os.environ.get("GOOGLE_SHEETS_DB_ID", "").strip()
LEADS_TAB = os.getenv("LEADS_TAB", "Leads")
LISTINGS_TAB = os.getenv("LISTINGS_TAB", "Listings")

CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()  # для публичного канала
MANAGER_CHAT_ID_RAW = os.getenv("MANAGER_CHAT_ID", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

def _parse_chat_id(s: str) -> Union[int, None]:
    if not s:
        return None
    if s.lstrip("-").isdigit():
        return int(s)
    return None

CHANNEL_ID = _parse_chat_id(CHANNEL_ID_RAW)
MANAGER_CHAT_ID = _parse_chat_id(MANAGER_CHAT_ID_RAW)

SYSTEM_PROMPT = (
    "Ты — Cozy Asia Consultant, дружелюбный и чёткий помощник по аренде/покупке недвижимости на Самуи. "
    "Отвечай кратко и по делу; если сведений не хватает — задай 1 уточняющий вопрос. "
    "Приоритет — предлагать варианты из внутренней базы (таблица Listings)."
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

def leads_append(row: List):
    ws = get_ws(LEADS_TAB, LEAD_HEADERS)
    ws.append_row(row)

# ---------- Listings upsert (по message_id) ----------
def listings_upsert_by_message(parsed: Dict[str, str], message_id: str):
    """
    Upsert в таб Listings по полю message_id.
    parsed: id,title,area,bedrooms,price_thb,link,message_id,status,notes
    Пустые значения не перезаписывают существующие.
    """
    ws = get_ws(LISTINGS_TAB, LISTING_HEADERS)
    header = ws.row_values(1)
    if not header:
        ws.append_row(LISTING_HEADERS)
        header = LISTING_HEADERS
    col_map = {name: idx+1 for idx, name in enumerate(header)}

    msg_col = col_map.get("message_id")
    existing_ids = ws.col_values(msg_col) if msg_col else []
    target_row = None
    for r, val in enumerate(existing_ids, start=1):
        if r == 1:
            continue
        if str(val).strip() == str(message_id):
            target_row = r
            break

    if target_row is None:
        new_row = [""] * len(header)
        for k, v in parsed.items():
            if k in col_map and str(v) != "":
                new_row[col_map[k]-1] = str(v)
        ws.append_row(new_row)
        log.info("Listings: appended new row for message_id=%s", message_id)
        return

    updates = []
    for k, v in parsed.items():
        if k in col_map and str(v) != "":
            updates.append((col_map[k], str(v)))

    if updates:
        cell_list = ws.range(target_row, 1, target_row, len(header))
        for col_idx, value in updates:
            cell_list[col_idx-1].value = value
        ws.update_cells(cell_list, value_input_option="USER_ENTERED")
        log.info("Listings: updated row %s for message_id=%s", target_row, message_id)

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

def build_perma(chat_id: int, message_id: int) -> str:
    if CHANNEL_USERNAME:
        return f"https://t.me/{CHANNEL_USERNAME}/{message_id}"
    s = str(chat_id)
    private_id = s[4:] if s.startswith("-100") else str(abs(chat_id))
    return f"https://t.me/c/{private_id}/{message_id}"

def listing_link(item: Dict) -> str:
    if item.get("link"):
        return str(item["link"])
    mid = str(item.get("message_id") or "").strip()
    if mid:
        if CHANNEL_USERNAME:
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

# -------- Поиск по Listings --------
def search_listings(area: str = "", bedrooms: int = 0, budget_thb: int = 0,
                    pets: Optional[bool] = None, limit: int = 3) -> List[Dict]:
    items = listings_all()
    out = []
    for it in items:
        if str(it.get("status","")).lower() in ["sold","inactive","hidden","закрыто","продано","сдано"]:
            continue
        if area and (area.lower() not in str(it.get("area","")).lower()):
            continue
        if bedrooms and to_int(str(it.get("bedrooms","0"))) < bedrooms:
            continue
        if budget_thb and to_int(str(it.get("price_thb","0"))) > budget_thb:
            continue
        if pets is True and str(it.get("pets","")).strip().lower() not in ["yes","да","true","разрешены","allowed"]:
            continue
        out.append(it)
    out.sort(key=lambda x: to_int(str(x.get("price_thb","0"))))
    return out[:max(1, min(limit, 10))]

# ---------- OpenAI ----------
def _chat_completion(messages: List[Dict]) -> str:
    if not OPENAI_API_KEY:
        return "У меня нет доступа к OpenAI, добавьте OPENAI_API_KEY в Render → Environment."
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": OPENAI_MODEL, "messages": messages, "temperature": 0.3, "max_tokens": 500}
    r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=OPENAI_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()

async def ai_answer(prompt: str) -> str:
    msgs = [{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":prompt}]
    return await asyncio.to_thread(_chat_completion, msgs)

# ---------- Preflight: мягкий сброс long-poll ----------
def preflight_release_slot(token: str, attempts: int = 6):
    base = f"https://api.telegram.org/bot{token}"

    def _post(method, **params):
        try:
            r = requests.post(f"{base}/{method}", params=params, timeout=10)
            return r.status_code
        except Exception as e:
            log.warning("%s error: %s", method, e)
            return None

    _post("deleteWebhook", drop_pending_updates=True)
    _post("close")

    backoff = 2
    for i in range(1, attempts + 1):
        try:
            chk = requests.get(f"{base}/getUpdates", params={"timeout": 1}, timeout=5)
            if chk.status_code != 409:
                log.info("Polling slot is free (status %s).", chk.status_code)
                return
            log.warning("409 Conflict still present (try %d/%d)", i, attempts)
        except Exception as e:
            log.warning("getUpdates check error: %s", e)
        time.sleep(backoff)
        backoff = min(backoff * 2, 8)
    log.warning("Polling slot may still be busy, starting anyway…")

# ---------- Парсер постов канала ----------
AREAS = {
    "lamai":   ["lamai", "ламай"],
    "bophut":  ["bophut", "бопут", "бо пут", "бофут", "бопхут"],
    "maenam":  ["maenam", "маенам", "ме нам"],
    "chaweng": ["chaweng", "чавенг", "чавеньг"],
    "bangrak": ["bang rak", "bangrak", "банграк", "бан рак"],
    "lipanoi": ["lipa", "lipa noi", "липа", "липа ной"],
}
LOT_RE   = re.compile(r"(?:лот|lot)\s*№?\s*(\d+)", re.I)
BED_RE   = re.compile(r"(\d+)\s*(?:спальн|спальни|bed(?:room)?s?|br)\b", re.I)
PRICE_RE = re.compile(r"(\d[\d\s'.,]{3,})\s*(?:฿|бат|thb)", re.I)

def norm_area(text: str) -> Optional[str]:
    t = (text or "").lower()
    for canon, aliases in AREAS.items():
        for a in aliases:
            if a in t:
                return canon
    return None

def parse_channel_text(text: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not text:
        return data
    if (m := LOT_RE.search(text)):
        data["id"] = m.group(1)  # лот кладём как id
        data["title"] = f"Лот №{m.group(1)}"
    if (m := BED_RE.search(text)):
        data["bedrooms"] = str(int(m.group(1)))
    ar = norm_area(text)
    if ar:
        data["area"] = ar
    if (m := PRICE_RE.search(text)):
        p = re.sub(r"[^\d]", "", m.group(1))
        if p:
            data["price_thb"] = p
    return data

# ---------- States ----------
(ASK_AREA, ASK_BEDROOMS, ASK_GUESTS, ASK_PETS, ASK_BUDGET,
 ASK_CHECKIN, ASK_CHECKOUT, ASK_TRANSFER, ASK_NAME, ASK_PHONE, ASK_REQS, DONE) = range(12)

# ---------- Handlers: /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Я уже тут!\n"
        "🌴 Можете спросить меня о Самуи — подскажу и помогу.\n\n"
        "👉 Или нажми /rent — задам несколько вопросов, сформирую заявку и предложу варианты ✨"
    )

# ---------- Wizard /rent ----------
async def rent_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Начнём. Какой район Самуи предпочитаете? (Маенам, Бопут, Чавенг, Ламай)")
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
    await update.message.reply_text("Контактный телефон (с кодом страны)?")
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["phone"] = update.message.text.strip()
    await update.message.reply_text("Доптребования? (вид на море, бассейн, рабочее место и т.д.)")
    return ASK_REQS

async def finish_lead(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["requirements"] = update.message.text.strip()
    area = context.user_data.get("area","")
    bedrooms = int(context.user_data.get("bedrooms",1))
    budget = int(context.user_data.get("budget_thb",0))
    pets = context.user_data.get("pets", False)

    try:
        matches = search_listings(area=area, bedrooms=bedrooms, budget_thb=budget, pets=pets, limit=3)
    except Exception as e:
        log.exception("Search listings error: %s", e)
        matches = []

    if matches:
        await update.message.reply_text("Подобрал варианты из нашей базы (первые 3):")
        for it in matches:
            for chunk in chunk_text(format_listing(it)):
                await update.message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
    else:
        await update.message.reply_text("Пока точных совпадений не нашёл. Передам менеджеру ваш запрос — он предложит индивидуальные варианты.")

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

# ---------- AI fallback ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    prompt = update.message.text.strip()
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    if re.search(r"(help|подобрать|найти|дом|вил+а|квартира|аренда)", prompt.lower()):
        await update.message.reply_text("Могу запустить быстрый опрос и предложить варианты из базы. Напишите /rent.")

    if not OPENAI_API_KEY:
        return
    try:
        reply = await ai_answer(prompt)
        for chunk in chunk_text(reply):
            await update.message.reply_text(chunk)
    except Exception as e:
        log.exception("OpenAI error: %s", e)

# ---------- Постинг в канал ----------
async def post_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Недостаточно прав.")
        return
    if CHANNEL_ID is None:
        await update.message.reply_text("❗️CHANNEL_ID не задан в Environment.")
        return
    text = " ".join(context.args).strip() or "Тест из бота 🚀"
    try:
        await context.bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode=ParseMode.HTML)
        await update.message.reply_text("✅ Отправил в канал.")
    except Exception as e:
        log.exception("Post to channel error: %s", e)
        await update.message.reply_text("Не удалось отправить в канал (см. логи).")

# ---------- Индексация канала → Listings ----------
def _extract_limit(text: str, fallback: int = 3) -> int:
    nums = [int(n) for n in re.findall(r"\b(\d{1,2})\b", text)]
    if nums:
        return max(1, min(nums[-1], 10))
    return fallback

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.channel_post
    if not m:
        return
    text = m.text or m.caption or ""
    parsed = parse_channel_text(text)
    if not parsed:
        return
    parsed["message_id"] = str(m.message_id)
    parsed.setdefault("link", build_perma(m.chat.id, m.message_id))
    parsed.setdefault("status", "active")
    try:
        listings_upsert_by_message(parsed, message_id=str(m.message_id))
        log.info("Indexed channel post #%s into Listings", m.message_id)
    except Exception as e:
        log.exception("Listings upsert error: %s", e)

async def handle_channel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.edited_channel_post
    if not m:
        return
    text = m.text or m.caption or ""
    parsed = parse_channel_text(text)
    parsed["message_id"] = str(m.message_id)
    parsed.setdefault("link", build_perma(m.chat.id, m.message_id))
    try:
        listings_upsert_by_message(parsed, message_id=str(m.message_id))
        log.info("Updated channel post #%s into Listings", m.message_id)
    except Exception as e:
        log.exception("Listings upsert (edit) error: %s", e)

# ---------- Поиск /find и свободный запрос ----------
def extract_filters_free(text: str):
    t = text.lower()
    area = norm_area(t) or ""
    beds = None
    if (m := BED_RE.search(t)):
        beds = int(m.group(1))
    budget = 0
    if (m := re.search(r"до\s*(\d{1,3}(?:[ .]?\d{3})*|\d+)\s*[кk]?", t)):
        budget = to_int(m.group(1))
        if re.search(r"[кk]\b", m.group(0)): budget *= 1000
    elif (m := re.search(r"(\d{1,3}(?:[ .]?\d{3})*|\d+)\s*[кk]?\s*(?:бат|thb|฿)", t)):
        budget = to_int(m.group(1))
        if re.search(r"[кk]\b", m.group(0)): budget *= 1000
    pets = True if re.search(r"\b(питомц|pets|животн).*(да|разреш|ok|allowed)", t) else None
    limit = _extract_limit(t, 3)
    return area, (beds or 0), budget, pets, limit

async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args) if context.args else (update.message.text or "")
    area, bedrooms, budget, pets, limit = extract_filters_free(q)
    try:
        rows = search_listings(area=area, bedrooms=bedrooms, budget_thb=budget, pets=pets, limit=limit)
    except Exception as e:
        log.exception("search_listings error: %s", e)
        rows = []
    if not rows:
        await update.message.reply_text("Ничего не нашёл по этим параметрам 🙈 Уточните район/спальни/бюджет.")
        return
    text = "Нашёл варианты:\n\n" + "\n\n".join(format_listing(r) for r in rows)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)

async def free_text_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    txt = update.message.text
    if norm_area(txt) or BED_RE.search(txt) or "покажи" in txt.lower() or "найди" in txt.lower():
        area, bedrooms, budget, pets, limit = extract_filters_free(txt)
        try:
            rows = search_listings(area=area, bedrooms=bedrooms, budget_thb=budget, pets=pets, limit=limit)
        except Exception as e:
            log.exception("search_listings error: %s", e)
            rows = []
        if rows:
            text = "Подобрал варианты:\n\n" + "\n\n".join(format_listing(r) for r in rows)
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
            return
    await handle_text(update, context)

# ---------- ENTRY ----------
def main():
    preflight_release_slot(TOKEN)
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

    # /find
    app.add_handler(CommandHandler("find", cmd_find))

    # /post (только в личке и только админам)
    app.add_handler(CommandHandler("post", post_to_channel, filters=filters.ChatType.PRIVATE))

    # Индексация канала (новые/исправленные посты)
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST, handle_channel_edit))

    # Свободный текст → умный поиск → fallback к AI
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_find))

    log.info("🚀 Starting polling…")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "channel_post", "edited_channel_post"]
    )

if __name__ == "__main__":
    main()
