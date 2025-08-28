# main.py — Cozy Asia Bot (Render-friendly: webhook OR polling, PTB v21)
# Требуемые ENV:
#   TELEGRAM_BOT_TOKEN
#   GOOGLE_SHEETS_DB_ID
#   GOOGLE_SERVICE_ACCOUNT_JSON  (весь JSON одной строкой)
# Необязательные ENV:
#   PUBLIC_CHANNEL_USERNAME   (без @), MANAGER_CHAT_ID, GREETING_TEXT
#   BASE_URL, WEBHOOK_PATH, PORT  (для webhook; без BASE_URL → polling)

import os
import json
import re
import logging
import time
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ---------- LOG ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cozyasia-bot")

# ---------- ENV ----------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GOOGLE_SHEETS_DB_ID = os.environ["GOOGLE_SHEETS_DB_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

PUBLIC_CHANNEL_USERNAME = os.environ.get("PUBLIC_CHANNEL_USERNAME", "").lstrip("@")
MANAGER_CHAT_ID = int(os.environ.get("MANAGER_CHAT_ID", "0"))
GREETING_TEXT = os.environ.get(
    "GREETING_TEXT",
    "✅ Я уже тут!\n🌴 Могу помочь с проживанием на Самуи.\n"
    "Нажми /rent — задам пару вопросов и предложу варианты из базы."
)

# ВАЖНО: безопасно читаем BASE_URL (может отсутствовать).
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", f"/{TELEGRAM_BOT_TOKEN}")
PORT = int(os.environ.get("PORT", "10000"))

# ---------- GSPREAD ----------
def _gspread_client():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ],
    )
    return gspread.authorize(creds)

gc = _gspread_client()
sh = gc.open_by_key(GOOGLE_SHEETS_DB_ID)

def _get_ws_prefer(names: List[str]):
    for n in names:
        try:
            return sh.worksheet(n)
        except gspread.WorksheetNotFound:
            continue
    return None

def _ensure_ws_exact(name: str, header: List[str]):
    ws = _get_ws_prefer([name, name.lower(), name.upper(), name.capitalize()])
    if ws is None:
        ws = sh.add_worksheet(title=name, rows="2000", cols=str(len(header) + 5))
        ws.append_row(header)
    elif not ws.row_values(1):
        ws.append_row(header)
    return ws

# ---------- SHEETS SCHEMA ----------
WS_LISTINGS_HDR = [
    "listing_id","created_at","title","description","location","bedrooms","bathrooms",
    "price_month","pets_allowed","utilities","electricity_rate","water_rate",
    "area_m2","pool","furnished","link","images","tags","raw_text"
]
WS_LEADS_HDR = [
    "lead_id","created_at","user_id","username","query_text","location_pref",
    "budget_min","budget_max","bedrooms","pets","dates","matched_ids","status"
]

ws_listings = _ensure_ws_exact("Listings", WS_LISTINGS_HDR)
ws_leads    = _ensure_ws_exact("Leads",    WS_LEADS_HDR)

# ---------- UTILS / PARSERS ----------
def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

AREA_WORDS = {
    "lamai":     ["ламай","ламаи","lamai","lamay"],
    "bophut":    ["бофут","bophut","fisherman"],
    "chaweng":   ["чавенг","chaweng"],
    "maenam":    ["маенам","maenam"],
    "bangrak":   ["банграк","bangrak"],
    "choengmon": ["чонгмон","чоенгмон","choeng mon","choengmon"],
    "lipanoi":   ["липаной","lipa noi","lipanoi"],
    "nathon":    ["натон","nathon"],
}

def parse_num(s: str) -> Optional[int]:
    m = re.search(r"(\d[\d\s'.,]{2,})", s.replace("\u202f", " "))
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace("'", "").replace(",", "")
    try:
        return int(float(raw))
    except Exception:
        return None

def extract_location(t: str) -> str:
    t = t.lower()
    for k, vs in AREA_WORDS.items():
        if any(v in t for v in vs):
            return k
    return ""

def extract_bedrooms(t: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*(спальн|спальни|сп|br|bed|bedrooms?)", t.lower())
    return int(m.group(1)) if m else None

def extract_bathrooms(t: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*(сануз|bath|bathrooms?)", t.lower())
    return int(m.group(1)) if m else None

def extract_price_month(t: str) -> Optional[int]:
    t2 = t.lower().replace("к", "000")
    if any(x in t2 for x in ["бат","thb","฿","/мес","/month"]):
        return parse_num(t2)
    return parse_num(t2)

def extract_pets(t: str) -> str:
    t = t.lower()
    if "без животных" in t or "no pets" in t:
        return "FALSE"
    if "с питомц" in t or "pets ok" in t or "pets allowed" in t:
        return "TRUE"
    return "UNKNOWN"

def extract_rates(t: str) -> Tuple[Optional[float], Optional[float]]:
    el = water = None
    m1 = re.search(r"(\d+(?:[.,]\d+)?)\s*бат.?/?\s*квт", t.lower())
    if m1:
        el = float(m1.group(1).replace(",", "."))
    m2 = re.search(r"(\d+(?:[.,]\d+)?)\s*бат.?/?\s*м3", t.lower())
    if m2:
        water = float(m2.group(1).replace(",", "."))
    return el, water

def tme_link(chat_id: int, msg_id: int) -> str:
    if PUBLIC_CHANNEL_USERNAME:
        return f"https://t.me/{PUBLIC_CHANNEL_USERNAME}/{msg_id}"
    abs_id = str(chat_id).replace("-100", "")
    return f"https://t.me/c/{abs_id}/{msg_id}"

# ---------- SHEETS OPS ----------
def listings_all() -> List[Dict[str, Any]]:
    return ws_listings.get_all_records()

def listing_exists(listing_id: int) -> bool:
    return str(listing_id) in set(ws_listings.col_values(1)[1:])

def append_listing(row: Dict[str, Any]):
    ws_listings.append_row([
        row.get("listing_id",""), row.get("created_at",""), row.get("title",""),
        row.get("description",""), row.get("location",""), row.get("bedrooms",""),
        row.get("bathrooms",""), row.get("price_month",""), row.get("pets_allowed",""),
        row.get("utilities",""), row.get("electricity_rate",""), row.get("water_rate",""),
        row.get("area_m2",""), row.get("pool",""), row.get("furnished",""),
        row.get("link",""), row.get("images",""), row.get("tags",""), row.get("raw_text",""),
    ], value_input_option="RAW")

def append_lead(row: Dict[str, Any]) -> str:
    lead_id = f"L{int(time.time())}"
    ws_leads.append_row([
        lead_id, now_iso(),
        row.get("user_id",""), row.get("username",""),
        row.get("query_text",""), row.get("location_pref",""),
        row.get("budget_min",""), row.get("budget_max",""),
        row.get("bedrooms",""), row.get("pets",""), row.get("dates",""),
        row.get("matched_ids",""), row.get("status","new"),
    ], value_input_option="RAW")
    return lead_id

# ---------- MATCH ----------
def match_by_criteria(criteria: Dict[str, Any], items: List[Dict[str, Any]], top_k: int = 6) -> List[Dict[str, Any]]:
    loc = (criteria.get("location") or criteria.get("location_pref") or "").lower()
    budget_min = int(criteria.get("budget_min") or 0)
    budget_max = int(criteria.get("budget_max") or 10**9)
    br_need = int(criteria.get("bedrooms") or 0)
    pets = (criteria.get("pets") or "").upper()

    out = []
    for it in items:
        try:
            price = int(it.get("price_month") or 0)
            br    = int(it.get("bedrooms") or 0)
            itloc = (it.get("location") or "").lower()
            if price and not (budget_min <= price <= budget_max):
                continue
            if br_need and br < br_need:
                continue
            if pets == "TRUE" and str(it.get("pets_allowed","UNKNOWN")).upper() == "FALSE":
                continue
            score = 0.0
            if budget_max < 10**9 and price:
                mid = (budget_min + budget_max) / 2
                score -= abs(price - mid) / max(mid, 1)
            if loc and loc in itloc:
                score += 0.6
            if br >= br_need:
                score += 0.2
            it["_score"] = score
            out.append(it)
        except Exception:
            pass
    out.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return out[:top_k]

# ---------- CONVERSATION ----------
ASK_LOC, ASK_BUDGET, ASK_BEDS, ASK_PETS, ASK_DATES = range(5)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(GREETING_TEXT)

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш chat_id: {update.effective_chat.id}")

async def cmd_rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Какой район интересует? (например: Lamai, Bophut, Chaweng)")
    return ASK_LOC

async def ask_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["location_pref"] = update.message.text.strip()
    await update.message.reply_text("Бюджет в месяц? Можно диапазон (например: 40-60 тыс).")
    return ASK_BUDGET

async def ask_beds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    nums = re.findall(r"\d+", txt.replace(" ", ""))
    if len(nums) == 1:
        val = int(nums[0])
        val = val * 1000 if val < 1000 else val
        bmin, bmax = 0, val
    elif len(nums) >= 2:
        a, b = int(nums[0]), int(nums[1])
        if a < 1000: a *= 1000
        if b < 1000: b *= 1000
        bmin, bmax = min(a, b), max(a, b)
    else:
        bmin, bmax = 0, 10**9
    context.user_data["budget_min"] = bmin
    context.user_data["budget_max"] = bmax
    await update.message.reply_text("Сколько спален нужно?")
    return ASK_BEDS

async def ask_pets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    brs = re.findall(r"\d+", update.message.text)
    context.user_data["bedrooms"] = int(brs[0]) if brs else 0
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Да", callback_data="pets_yes"),
        InlineKeyboardButton("Нет", callback_data="pets_no")
    ]])
    await update.message.reply_text("С питомцами?", reply_markup=kb)
    return ASK_PETS

async def ask_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["pets"] = "TRUE" if q.data == "pets_yes" else "FALSE"
    await q.edit_message_text("На какие даты планируете заезд/срок аренды?")
    return ASK_DATES

async def finish_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["dates"] = update.message.text.strip()
    items = listings_all()
    crit = {
        "location_pref": context.user_data.get("location_pref",""),
        "budget_min":    context.user_data.get("budget_min",0),
        "budget_max":    context.user_data.get("budget_max",10**9),
        "bedrooms":      context.user_data.get("bedrooms",0),
        "pets":          context.user_data.get("pets","UNKNOWN"),
    }
    top = match_by_criteria(crit, items)
    matched_ids = ",".join([str(it.get("listing_id")) for it in top])

    lead_id = append_lead({
        "user_id": update.effective_user.id,
        "username": update.effective_user.username or "",
        "query_text": f"Анкета: {crit}",
        "location_pref": crit["location_pref"],
        "budget_min": crit["budget_min"],
        "budget_max": crit["budget_max"],
        "bedrooms": crit["bedrooms"],
        "pets": crit["pets"],
        "dates": context.user_data["dates"],
        "matched_ids": matched_ids
    })

    if top:
        lines = []
        for it in top:
            lines.append(
                f"• <b>{it.get('title') or 'Вилла/Дом'}</b>\n"
                f"{(it.get('location') or '').title()} | {it.get('bedrooms','?')} сп. | "
                f"{it.get('price_month','?')} бат/мес\n{it.get('link','')}"
            )
        await update.message.reply_html("Подобрал варианты:\n\n" + "\n\n".join(lines))
    else:
        await update.message.reply_text(
            "Пока не вижу подходящих лотов. Я передал запрос менеджеру — подберём вручную."
        )

    if MANAGER_CHAT_ID:
        await context.bot.send_message(
            MANAGER_CHAT_ID,
            f"Новая заявка {lead_id} от @{update.effective_user.username} ({update.effective_user.id})\n"
            f"Критерии: {crit}\nСовпадения: {matched_ids or 'нет'}"
        )
    return ConversationHandler.END

async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок, отменил.")
    return ConversationHandler.END

# ---------- FREE TEXT ----------
def heuristics_criteria(text: str) -> Dict[str, Any]:
    loc = extract_location(text)
    beds = extract_bedrooms(text) or 0
    budget_min, budget_max = 0, 10**9
    nums = re.findall(r"\d[\d\s]{1,}", text)
    if nums:
        vals = []
        for n in nums:
            v = int(re.sub(r"\D","", n))
            if v < 2000:
                v *= 1000
            vals.append(v)
        if len(vals) == 1:
            budget_max = vals[0]
        else:
            a, b = sorted(vals[:2])
            budget_min, budget_max = a, b
    pets = extract_pets(text)
    return {
        "location_pref": loc, "bedrooms": beds,
        "budget_min": budget_min, "budget_max": budget_max,
        "pets": pets
    }

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    crit = heuristics_criteria(txt)
    items = listings_all()
    top = match_by_criteria(crit, items)

    if top:
        lines = []
        for it in top:
            lines.append(
                f"• <b>{it.get('title') or 'Вилла/Дом'}</b>\n"
                f"{(it.get('location') or '').title()} | {it.get('bedrooms','?')} сп. | "
                f"{it.get('price_month','?')} бат/мес\n{it.get('link','')}"
            )
        await update.message.reply_html("Вот что подходит:\n\n" + "\n\n".join(lines))
    else:
        await update.message.reply_text(
            "Уточните, пожалуйста, район/бюджет/спальни — пока ничего не нашёл в базе."
        )

    append_lead({
        "user_id": update.effective_user.id,
        "username": update.effective_user.username or "",
        "query_text": txt,
        "location_pref": crit.get("location_pref",""),
        "budget_min": crit.get("budget_min",0),
        "budget_max": crit.get("budget_max",10**9),
        "bedrooms": crit.get("bedrooms",0),
        "pets": crit.get("pets","UNKNOWN"),
        "dates": "",
        "matched_ids": ",".join([str(it.get("listing_id")) for it in top])
    })

# ---------- CHANNEL POSTS -> SHEETS ----------
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    text = (msg.caption or msg.text or "").strip()
    if not text:
        return

    listing_id = msg.message_id
    if listing_exists(listing_id):
        return

    title = text.splitlines()[0][:120] if text else "Объект"
    desc  = "\n".join(text.splitlines()[1:])[:4000]
    location = extract_location(text)
    bedrooms = extract_bedrooms(text) or ""
    bathrooms = extract_bathrooms(text) or ""
    price_month = extract_price_month(text) or ""
    pets_allowed = extract_pets(text)
    el_rate, water_rate = extract_rates(text)

    imgs = []
    if msg.photo:
        imgs.append(msg.photo[-1].file_id)

    row = {
        "listing_id": listing_id, "created_at": now_iso(),
        "title": title, "description": desc,
        "location": location, "bedrooms": bedrooms, "bathrooms": bathrooms,
        "price_month": price_month, "pets_allowed": pets_allowed,
        "utilities": "", "electricity_rate": el_rate if el_rate is not None else "",
        "water_rate": water_rate if water_rate is not None else "",
        "area_m2": "", "pool": "UNKNOWN", "furnished": "UNKNOWN",
        "link": tme_link(msg.chat.id, msg.message_id),
        "images": ",".join(imgs), "tags": "", "raw_text": text
    }
    append_listing(row)
    log.info("Saved listing %s to Sheets", listing_id)

# ---------- BUILD APP ----------
async def post_init(app):
    me = await app.bot.get_me()
    log.info("Bot started: @%s", me.username)

def build_app():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Conversation — ПЕРВЫМ
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", cmd_rent)],
        states={
            ASK_LOC:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_budget)],
            ASK_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_beds)],
            ASK_BEDS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_pets)],
            ASK_PETS:   [CallbackQueryHandler(ask_dates)],
            ASK_DATES:  [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_flow)],
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Остальные handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app

# ---------- RUN ----------
def main():
    app = build_app()
    if BASE_URL:  # webhook-режим
        logging.info("Starting webhook on %s%s", BASE_URL, WEBHOOK_PATH)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=f"{BASE_URL}{WEBHOOK_PATH}",
            allowed_updates=["message","channel_post","callback_query"],
            drop_pending_updates=True,
        )
    else:         # polling-режим (если BASE_URL не задан)
        logging.info("BASE_URL not set -> starting polling")
        app.run_polling(
            allowed_updates=["message","channel_post","callback_query"],
            drop_pending_updates=True
        )

if __name__ == "__main__":
    main()
