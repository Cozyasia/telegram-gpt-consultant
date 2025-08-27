import os, json, re, logging, asyncio, time, math
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import requests
import gspread
from google.oauth2.service_account import Credentials
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatAction,
    InputMediaPhoto
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, filters
)

# ---------- ЛОГГЕР ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cozyasia-bot")

# ---------- ENV ----------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GOOGLE_SHEETS_DB_ID = os.environ["GOOGLE_SHEETS_DB_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
MANAGER_CHAT_ID = int(os.environ.get("MANAGER_CHAT_ID", "0"))
PUBLIC_CHANNEL_USERNAME = os.environ.get("PUBLIC_CHANNEL_USERNAME", "").lstrip("@")

# ---------- GSPREAD ----------
def _gspread_client():
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(creds)

gc = _gspread_client()
sh = gc.open_by_key(GOOGLE_SHEETS_DB_ID)

def _ensure_ws(name: str, header: List[str]):
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows="2000", cols=str(len(header)+5))
        ws.append_row(header)
    return ws

WS_LISTINGS_HDR = [
    "listing_id","created_at","title","description","location","bedrooms","bathrooms",
    "price_month","pets_allowed","available_from","utilities","electricity_rate",
    "water_rate","area_m2","pool","furnished","link","images","tags","raw_text"
]
WS_LEADS_HDR = [
    "lead_id","created_at","user_id","username","query_text","location_pref",
    "budget_min","budget_max","bedrooms","pets","dates","matched_ids","status"
]
ws_listings = _ensure_ws("listings", WS_LISTINGS_HDR)
ws_leads    = _ensure_ws("leads", WS_LEADS_HDR)

# ---------- УТИЛИТЫ ----------
def tme_link_for_message(chat_id: int, message_id: int) -> str:
    # Публичный канал -> t.me/<username>/<id>, приватный -> t.me/c/<abs_id>/<id>
    if PUBLIC_CHANNEL_USERNAME:
        return f"https://t.me/{PUBLIC_CHANNEL_USERNAME}/{message_id}"
    # приватные/супергруппы
    abs_id = str(chat_id).replace("-100", "")
    return f"https://t.me/c/{abs_id}/{message_id}"

def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

# Простая нормализация чисел "60'000 бат" -> 60000
def parse_num(text: str) -> Optional[int]:
    m = re.search(r"(\d[\d\s'.,]{2,})", text.replace("\u202f"," "))
    if not m: return None
    raw = m.group(1).replace(" ", "").replace("'", "").replace(",", "")
    try:
        return int(float(raw))
    except:
        return None

AREA_WORDS = {
    "lamai": ["ламай","lamai"], "bophut": ["бофут","bophut","fisherman"],
    "chaweng":["чавенг","chaweng"], "maenam":["маенам","maenam"],
    "bangrak":["bangrak","банграк"], "choengmon":["choeng mon","чоенгмон","чонгмон","choengmon"],
    "lipanoi":["липаной","lipa noi","lipanoi"], "nathon":["натон","nathon"]
}

def extract_location(text: str) -> str:
    t = text.lower()
    for key, variants in AREA_WORDS.items():
        if any(v in t for v in variants):
            return key
    return ""

def extract_bedrooms(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*(спальн|спальни|сп|br|bed|bedrooms?)", text.lower())
    return int(m.group(1)) if m else None

def extract_bathrooms(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*(сануз|bath|bathrooms?)", text.lower())
    return int(m.group(1)) if m else None

def extract_price_month(text: str) -> Optional[int]:
    # ищем "55 000 бат", "55k", "60,000", "60 000 THB", "/month"
    t = text.lower().replace("к","000")
    if "бат" in t or "thb" in t or "฿" in t or "/мес" in t or "/month" in t:
        return parse_num(t)
    # fallback: просто первое «большое» число
    return parse_num(t)

def extract_pets(text: str) -> str:
    t = text.lower()
    if "без животных" in t or "no pets" in t: return "FALSE"
    if "с питомц" in t or "pets ok" in t or "pets allowed" in t: return "TRUE"
    return "UNKNOWN"

def extract_available_from(text: str) -> str:
    m = re.search(r"(от|from)\s+(\d{1,2}\s*[а-яa-z]{3,}|[0-9]{4}-[0-9]{2}-[0-9]{2}|сент|окт|nov|dec)", text.lower())
    return m.group(0) if m else ""

def extract_rates(text: str) -> Tuple[Optional[float], Optional[float]]:
    el = None; water = None
    m1 = re.search(r"(\d+(?:[.,]\d+)?)\s*бат.?/?\s*квт", text.lower())
    if m1:
        el = float(m1.group(1).replace(",","."))
    m2 = re.search(r"(\d+(?:[.,]\d+)?)\s*бат.?/?\s*м3", text.lower())
    if m2:
        water = float(m2.group(1).replace(",","."))
    return el, water

def extract_bool(text: str, words_true: List[str], words_false: List[str]) -> str:
    t = text.lower()
    if any(w in t for w in words_true): return "TRUE"
    if any(w in t for w in words_false): return "FALSE"
    return "UNKNOWN"

# Опциональный GPT-парсер к структуре
def llm_extract(text: str) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        return {}
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        sys_prompt = (
            "Ты извлекаешь параметры аренды из текста объявления Самуи. "
            "Верни JSON с ключами: title, location, bedrooms, bathrooms, price_month, "
            "pets_allowed(TRUE/FALSE/UNKNOWN), available_from, utilities, electricity_rate, "
            "water_rate, area_m2, pool(TRUE/FALSE/UNKNOWN), furnished(TRUE/FALSE/UNKNOWN), tags[]."
        )
        user = f"Текст:\n{text}"
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys_prompt},{"role":"user","content":user}],
            temperature=0
        )
        j = resp["choices"][0]["message"]["content"]
        # на случай если пришел текст с блоком кода
        j = re.sub(r"^```json|```$", "", j.strip(), flags=re.MULTILINE)
        return json.loads(j)
    except Exception as e:
        log.warning("LLM extract fail: %s", e)
        return {}

# ---------- РАБОТА С SHEETS ----------
def listings_all() -> List[Dict[str,Any]]:
    rows = ws_listings.get_all_records()
    return rows

def listing_exists(listing_id: int) -> bool:
    # Быстрое O(n). Для больших объемов можно кэшировать в set
    cells = ws_listings.col_values(1)[1:]  # без заголовка
    return str(listing_id) in set(cells)

def append_listing(row: Dict[str,Any]):
    values = [
        row.get("listing_id",""),
        row.get("created_at",""),
        row.get("title",""),
        row.get("description",""),
        row.get("location",""),
        row.get("bedrooms",""),
        row.get("bathrooms",""),
        row.get("price_month",""),
        row.get("pets_allowed",""),
        row.get("available_from",""),
        row.get("utilities",""),
        row.get("electricity_rate",""),
        row.get("water_rate",""),
        row.get("area_m2",""),
        row.get("pool",""),
        row.get("furnished",""),
        row.get("link",""),
        row.get("images",""),
        row.get("tags",""),
        row.get("raw_text",""),
    ]
    ws_listings.append_row(values, value_input_option="RAW")

def append_lead(row: Dict[str,Any]) -> str:
    lead_id = f"L{int(time.time())}"
    values = [
        lead_id, now_iso(),
        row.get("user_id",""), row.get("username",""),
        row.get("query_text",""), row.get("location_pref",""),
        row.get("budget_min",""), row.get("budget_max",""),
        row.get("bedrooms",""), row.get("pets",""),
        row.get("dates",""), row.get("matched_ids",""),
        row.get("status","new")
    ]
    ws_leads.append_row(values, value_input_option="RAW")
    return lead_id

# ---------- МАТЧИНГ ----------
def match_by_criteria(criteria: Dict[str,Any], items: List[Dict[str,Any]], top_k:int=6) -> List[Dict[str,Any]]:
    loc = (criteria.get("location") or criteria.get("location_pref") or "").lower()
    budget_min = int(criteria.get("budget_min") or 0)
    budget_max = int(criteria.get("budget_max") or 10**9)
    br_need = int(criteria.get("bedrooms") or 0)
    pets = (criteria.get("pets") or "").upper()
    out = []
    for it in items:
        try:
            price = int(it.get("price_month") or 0)
            br = int(it.get("bedrooms") or 0)
            it_loc = (it.get("location") or "").lower()

            # фильтры
            if price and not (budget_min <= price <= budget_max): continue
            if br_need and br < br_need: continue
            if loc and loc not in it_loc:  # мягко
                # допускаем синонимы: lamai inside "lamai"
                pass
            if pets == "TRUE" and (str(it.get("pets_allowed","UNKNOWN")).upper() == "FALSE"):
                continue

            # скоринг близости
            score = 0
            if price:
                mid = (budget_min+budget_max)/2 if budget_max<10**9 else max(budget_min,1)
                score -= abs(price - mid) / max(mid,1)
            score += (br >= br_need) * 0.2
            if loc and loc in it_loc: score += 0.5

            it["_score"] = score
            out.append(it)
        except:
            continue
    out.sort(key=lambda x: x.get("_score",0), reverse=True)
    return out[:top_k]

# ---------- АНКЕТА ----------
ASK_LOC, ASK_BUDGET, ASK_BEDS, ASK_PETS, ASK_DATES = range(5)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я ассистент Cozy Asia 🏝️\n"
        "Напиши, что ищешь (район, бюджет, спальни, с питомцами и т.д.) или нажми /rent — подберу варианты из базы."
    )

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
    if len(nums)==1: 
        bmin=bmax=int(nums[0])*1000 if int(nums[0])<1000 else int(nums[0])
    elif len(nums)>=2:
        a,b = int(nums[0]), int(nums[1])
        if a<1000: a*=1000
        if b<1000: b*=1000
        bmin,bmax = min(a,b), max(a,b)
    else:
        bmin,bmax = 0, 10**9
    context.user_data["budget_min"]=bmin
    context.user_data["budget_max"]=bmax
    await update.message.reply_text("Сколько спален нужно?")
    return ASK_BEDS

async def ask_pets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bedrooms"] = int(re.findall(r"\d+", update.message.text)[0]) if re.findall(r"\d+", update.message.text) else 0
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Да", callback_data="pets_yes"),
                                InlineKeyboardButton("Нет", callback_data="pets_no")]])
    await update.message.reply_text("С питомцами?", reply_markup=kb)
    return ASK_PETS

async def ask_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["pets"] = "TRUE" if query.data=="pets_yes" else "FALSE"
    await query.edit_message_text("На какие даты планируете заезд/срок аренды?")
    return ASK_DATES

async def finish_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["dates"] = update.message.text.strip()
    # Матчим
    items = listings_all()
    crit = {
        "location_pref": context.user_data.get("location_pref",""),
        "budget_min": context.user_data.get("budget_min",0),
        "budget_max": context.user_data.get("budget_max",10**9),
        "bedrooms": context.user_data.get("bedrooms",0),
        "pets": context.user_data.get("pets","UNKNOWN"),
    }
    top = match_by_criteria(crit, items)
    matched_ids = ",".join([str(it.get("listing_id")) for it in top])

    lead_row = {
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
    }
    lead_id = append_lead(lead_row)

    # Ответ клиенту
    if not top:
        await update.message.reply_text("Пока не вижу подходящих лотов. Я передал запрос менеджеру — подберем вручную.")
    else:
        lines = []
        for it in top:
            lines.append(
                f"• <b>{it.get('title') or 'Вилла/Дом'}</b>\n"
                f"{it.get('location','').title()} | {it.get('bedrooms','?')} сп. | "
                f"{it.get('price_month','?')} бат/мес\n{it.get('link','')}"
            )
        await update.message.reply_html("Подобрал варианты:\n\n" + "\n\n".join(lines))

    # Менеджеру
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

# ---------- СВОБОДНЫЙ ТЕКСТ: ПАРСИНГ КРИТЕРИЕВ И ВЫДАЧА ----------
def heuristics_criteria(text: str) -> Dict[str,Any]:
    loc = extract_location(text)
    beds = extract_bedrooms(text) or 0
    # бюджет: ищем числа и слова "бат/тыс"
    budget_min, budget_max = 0, 10**9
    nums = re.findall(r"\d[\d\s]{1,}", text)
    if nums:
        vals = []
        for n in nums:
            v = int(re.sub(r"\D","", n))
            if v < 2000: v *= 1000
            vals.append(v)
        if len(vals)==1:
            budget_min, budget_max = 0, vals[0]
        else:
            a,b = sorted(vals[:2])
            budget_min, budget_max = a, b
    pets = extract_pets(text)
    return {"location_pref": loc, "bedrooms": beds, "budget_min": budget_min, "budget_max": budget_max, "pets": pets}

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
                f"{it.get('location','').title()} | {it.get('bedrooms','?')} сп. | "
                f"{it.get('price_month','?')} бат/мес\n{it.get('link','')}"
            )
        await update.message.reply_html("Вот что подходит:\n\n" + "\n\n".join(lines))
    else:
        await update.message.reply_text("Уточните, пожалуйста, район/бюджет/спальни — пока ничего не нашёл в базе.")

    # Запишем лид
    lead_row = {
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
    }
    append_lead(lead_row)
    if MANAGER_CHAT_ID:
        await context.bot.send_message(
            MANAGER_CHAT_ID,
            f"Запрос от @{update.effective_user.username}: {txt}\nПодбор: {lead_row['matched_ids'] or 'нет'}"
        )

# ---------- ПРИЁМ НОВЫХ ПУБЛИКАЦИЙ ИЗ КАНАЛА ----------
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    text = (msg.caption or msg.text or "").strip()
    if not text:
        return

    listing_id = msg.message_id
    if listing_exists(listing_id):
        return

    title = text.splitlines()[0][:120] if text else "Объект"
    desc = "\n".join(text.splitlines()[1:])[:4000]

    # базовый парс
    location = extract_location(text)
    bedrooms = extract_bedrooms(text) or ""
    bathrooms = extract_bathrooms(text) or ""
    price_month = extract_price_month(text) or ""
    pets_allowed = extract_pets(text)
    available_from = extract_available_from(text)
    el_rate, water_rate = extract_rates(text)

    # LLM обогащение (если доступен ключ)
    extra = llm_extract(text)

    # картинки
    imgs = []
    if msg.photo:
        # берем самый большой
        ph = msg.photo[-1]
        imgs.append(ph.file_id)

    link = tme_link_for_message(msg.chat.id, msg.message_id)

    row = {
        "listing_id": listing_id,
        "created_at": now_iso(),
        "title": extra.get("title") or title,
        "description": desc,
        "location": (extra.get("location") or location),
        "bedrooms": extra.get("bedrooms") or bedrooms,
        "bathrooms": extra.get("bathrooms") or bathrooms,
        "price_month": extra.get("price_month") or price_month,
        "pets_allowed": (extra.get("pets_allowed") or pets_allowed),
        "available_from": extra.get("available_from") or available_from,
        "utilities": extra.get("utilities") or "",
        "electricity_rate": extra.get("electricity_rate") or (el_rate if el_rate is not None else ""),
        "water_rate": extra.get("water_rate") or (water_rate if water_rate is not None else ""),
        "area_m2": extra.get("area_m2") or "",
        "pool": extra.get("pool") or "UNKNOWN",
        "furnished": extra.get("furnished") or "UNKNOWN",
        "link": link,
        "images": ",".join(imgs),
        "tags": ",".join(extra.get("tags", [])),
        "raw_text": text
    }
    append_listing(row)
    log.info("Saved listing %s to Sheets", listing_id)

# ---------- СЛУЖЕБНЫЕ КОМАНДЫ ----------
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш chat_id: {update.effective_chat.id}")

async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # просто проверка доступа
    if MANAGER_CHAT_ID and update.effective_user.id != MANAGER_CHAT_ID:
        await update.message.reply_text("Команда доступна менеджеру.")
        return
    # Ничего не делаем — база тянется из Sheets по факту; можно добавить кеш/эмбеддинги
    await update.message.reply_text("Ок. Кеш обновится при следующем запросе.")

# ---------- CONV HANDLER ----------
conv = ConversationHandler(
    entry_points=[CommandHandler("rent", cmd_rent)],
    states={
        ASK_LOC: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_budget)],
        ASK_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_beds)],
        ASK_BEDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_pets)],
        ASK_PETS: [MessageHandler(filters.UpdateType.CALLBACK_QUERY, ask_dates)],
        ASK_DATES: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_flow)],
    },
    fallbacks=[CommandHandler("cancel", cancel_flow)],
    allow_reentry=True,
)

# ---------- MAIN ----------
def preflight_release_slot(token: str, attempts: int = 5):
    base = f"https://api.telegram.org/bot{token}"
    try:
        requests.post(f"{base}/deleteWebhook", params={"drop_pending_updates": True}, timeout=10)
        log.info("deleteWebhook -> OK")
    except Exception as e:
        log.warning("deleteWebhook error: %s", e)
    for i in range(attempts):
        try:
            r = requests.post(f"{base}/close", timeout=10)
            log.info("close -> %s", r.json())
            time.sleep(1.2)
        except Exception as e:
            log.warning("close error: %s", e)

async def post_init(app):
    log.info("Bot started. Username: %s", (await app.bot.get_me()).username)

def main():
    preflight_release_slot(TELEGRAM_BOT_TOKEN)
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("refresh_cache", cmd_refresh))
    app.add_handler(conv)

    # канал: новые посты -> в Sheets
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post))
    # приватные чаты: свободный текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Polling...")
    app.run_polling(allowed_updates=["message","channel_post","callback_query"])

if __name__ == "__main__":
    main()
