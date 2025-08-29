# main.py
import os
import json
import logging
import datetime as dt
from typing import Dict, Any, Optional

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, CallbackContext, filters
)

# ====== OpenAI ======
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # чтобы логично отработать, если пакет не подтянулся

# ====== Google Sheets ======
import gspread

# ====== Даты (мягкий парсинг) ======
try:
    # намного надёжнее — умеет "01.12.2025", "2026/01/01", "1 Jan 2026", и т.п.
    import dateutil.parser as dparser
    _HAS_DATEUTIL = True
except Exception:
    _HAS_DATEUTIL = False

# ----------------- ЛОГИ -----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cozyasia-bot")

# ----------------- ENV -----------------
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("ENV TELEGRAM_TOKEN is required")

BASE_URL         = os.getenv("BASE_URL") or os.getenv("WEBHOOK_BASE")
WEBHOOK_PATH     = os.getenv("WEBHOOK_PATH", "/webhook")
PORT             = int(os.getenv("PORT", "10000"))
GROUP_CHAT_ID    = int(os.getenv("GROUP_CHAT_ID", "0"))

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_APIKEY") or os.getenv("OPENAI_KEY")
OPENAI_PROJECT   = os.getenv("OPENAI_PROJECT")
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

GS_JSON          = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GS_SHEET_ID      = os.getenv("GOOGLE_SHEETS_DB_ID")
LEADS_SHEET_NAME = os.getenv("GOOGLE_SHEETS_LEADS_SHEET", "Leads")

# ----------------- OpenAI клиент -----------------
def get_openai_client() -> Optional[Any]:
    if not OpenAI or not OPENAI_API_KEY:
        return None
    try:
        if OPENAI_PROJECT:
            return OpenAI(api_key=OPENAI_API_KEY, project=OPENAI_PROJECT)
        return OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        logger.exception("OpenAI init failed: %s", e)
        return None

oa_client = get_openai_client()

# ----------------- Google Sheets -----------------
def _get_ws():
    if not GS_JSON:
        raise RuntimeError("ENV GOOGLE_SERVICE_ACCOUNT_JSON is required")
    if not GS_SHEET_ID:
        raise RuntimeError("ENV GOOGLE_SHEETS_DB_ID is required")

    try:
        creds_info = json.loads(GS_JSON)
    except json.JSONDecodeError:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON: invalid JSON")

    gc = gspread.service_account_from_dict(creds_info)
    sh = gc.open_by_key(GS_SHEET_ID)

    try:
        ws = sh.worksheet(LEADS_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=LEADS_SHEET_NAME, rows=2000, cols=20)
        ws.append_row([
            "created_at", "chat_id", "username",
            "location", "bedrooms", "budget",
            "people", "pets",
            "check_in", "check_out",
            "notes", "type"
        ])
    return ws

def _ensure_headers(ws, wanted_headers):
    current = ws.row_values(1)
    to_add  = [h for h in wanted_headers if h not in current]
    if to_add:
        ws.update([current + to_add], '1:1')

def save_lead_to_sheet(lead: Dict[str, Any]) -> None:
    ws = _get_ws()
    wanted_headers = [
        "created_at", "chat_id", "username",
        "location", "bedrooms", "budget",
        "people", "pets",
        "check_in", "check_out",
        "notes", "type"
    ]
    _ensure_headers(ws, wanted_headers)
    headers = ws.row_values(1)
    row = []
    for h in headers:
        val = lead.get(h, "")
        if isinstance(val, (dt.date, dt.datetime)):
            val = val.strftime("%Y-%m-%d")
        row.append(val)
    ws.append_row(row, value_input_option="USER_ENTERED")

# ----------------- Вспомогательное -----------------
def friendly_links_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Открыть сайт", url="https://cozy.asia")],
        [InlineKeyboardButton("📣 Телеграм-канал (все лоты)", url="https://t.me/SamuiRental")],
        [InlineKeyboardButton("🏡 Канал по виллам", url="https://t.me/arenda_vill_samui")],
        [InlineKeyboardButton("📷 Instagram", url="https://www.instagram.com/cozy.asia")]
    ])

PROMO_TEXT = (
    "🔧 Самый действенный способ — пройти короткую анкету командой /rent.\n"
    "Я сделаю подборку лотов (дома/апартаменты/виллы) по вашим критериям и сразу отправлю вам. "
    "Менеджер получит вашу заявку и свяжется для уточнений.\n\n"
    "• Сайт: https://cozy.asia\n"
    "• Канал с лотами: https://t.me/SamuiRental\n"
    "• Канал по виллам: https://t.me/arenda_vill_samui\n"
    "• Instagram: https://www.instagram.com/cozy.asia"
)

def parse_date_loose(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None
    if _HAS_DATEUTIL:
        try:
            d = dparser.parse(text, dayfirst=True, yearfirst=False, fuzzy=True)
            return d.strftime("%Y-%m-%d")
        except Exception:
            pass
    # Фоллбек — популярные форматы
    fmts = [
        "%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y",
        "%Y.%m.%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y",
        "%d.%m.%y", "%d-%m-%y", "%Y.%m.%d", "%Y/%m/%d",
        "%Y%m%d"
    ]
    for f in fmts:
        try:
            d = dt.datetime.strptime(text, f)
            return d.strftime("%Y-%m-%d")
        except Exception:
            continue
    return None

# ----------------- СТЕЙТЫ АНКЕТЫ -----------------
(Q_TYPE, Q_BUDGET, Q_AREA, Q_BEDROOMS, Q_CHECKIN, Q_CHECKOUT, Q_NOTES) = range(7)

def start_text() -> str:
    return (
        "✅ Я уже тут!\n"
        "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n\n"
        "👉 Или нажмите команду /rent — задам несколько вопросов о жилье, "
        "сформирую заявку, предложу варианты и передам менеджеру. Он свяжется с вами для уточнения."
    )

# ----------------- ХЕНДЛЕРЫ КОМАНД -----------------
async def on_start(update: Update, context: CallbackContext):
    await update.effective_chat.send_message(start_text())
    await update.effective_chat.send_message(PROMO_TEXT, reply_markup=friendly_links_keyboard())

async def on_cancel(update: Update, context: CallbackContext):
    context.user_data.pop("lead", None)
    context.user_data["in_form"] = False
    await update.message.reply_text("Окей, если передумаете — пишите /rent.")

# --- Анкета ---
async def rent_start(update: Update, context: CallbackContext):
    context.user_data["lead"] = {}
    context.user_data["in_form"] = True
    await update.message.reply_text("Начнём подбор.\n1/7. Какой тип жилья интересует: квартира, дом или вилла?")
    return Q_TYPE

async def q_type(update: Update, context: CallbackContext):
    context.user_data["lead"]["type"] = update.message.text.strip().title()
    await update.message.reply_text("2/7. Какой у вас бюджет в батах (месяц)?")
    return Q_BUDGET

async def q_budget(update: Update, context: CallbackContext):
    context.user_data["lead"]["budget"] = "".join(ch for ch in update.message.text if ch.isdigit()) or update.message.text
    await update.message.reply_text("3/7. В каком районе Самуи предпочтительно жить?")
    return Q_AREA

async def q_area(update: Update, context: CallbackContext):
    context.user_data["lead"]["location"] = update.message.text.strip().title()
    await update.message.reply_text("4/7. Сколько нужно спален?")
    return Q_BEDROOMS

async def q_bedrooms(update: Update, context: CallbackContext):
    context.user_data["lead"]["bedrooms"] = "".join(ch for ch in update.message.text if ch.isdigit()) or update.message.text
    await update.message.reply_text("5/7. Дата **заезда**? Можно в любом формате (напр., 01.12.2025).")
    return Q_CHECKIN

async def q_checkin(update: Update, context: CallbackContext):
    parsed = parse_date_loose(update.message.text)
    if not parsed:
        await update.message.reply_text("Не понял дату. Попробуйте ещё раз — можно 01.12.2025 или 2025-12-01.")
        return Q_CHECKIN
    context.user_data["lead"]["check_in"] = parsed
    await update.message.reply_text("6/7. Дата **выезда**?")
    return Q_CHECKOUT

async def q_checkout(update: Update, context: CallbackContext):
    parsed = parse_date_loose(update.message.text)
    if not parsed:
        await update.message.reply_text("Не понял дату. Попробуйте ещё раз — можно 01.01.2026 или 2026-01-01.")
        return Q_CHECKOUT
    context.user_data["lead"]["check_out"] = parsed
    await update.message.reply_text("7/7. Важные условия? (близость к пляжу, с питомцами, парковка и т.п.)")
    return Q_NOTES

async def q_notes(update: Update, context: CallbackContext):
    context.user_data["lead"]["notes"] = update.message.text.strip()

    # Сформировать итоговую заявку
    u = update.effective_user
    lead = context.user_data["lead"]
    lead_full = {
        "created_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "chat_id": str(u.id),
        "username": (u.username or "").strip(),
        "location": lead.get("location", ""),
        "bedrooms": lead.get("bedrooms", ""),
        "budget": lead.get("budget", ""),
        "people": "",  # если понадобится — добавь вопрос
        "pets": "",
        "check_in": lead.get("check_in", ""),
        "check_out": lead.get("check_out", ""),
        "notes": lead.get("notes", ""),
        "type": lead.get("type", "")
    }

    # 1) Уведомляем рабочую группу
    if GROUP_CHAT_ID != 0:
        text = (
            "🆕 *Новая заявка Cozy Asia*\n"
            f"Клиент: @{u.username or '—'} (ID: {u.id})\n"
            f"Тип: {lead_full['type'] or '—'}\n"
            f"Район: {lead_full['location'] or '—'}\n"
            f"Бюджет: {lead_full['budget'] or '—'}\n"
            f"Спален: {lead_full['bedrooms'] or '—'}\n"
            f"Check-in: {lead_full['check_in'] or '—'}  |  Check-out: {lead_full['check_out'] or '—'}\n"
            f"Условия/прим.: {lead_full['notes'] or '—'}\n"
            f"Создано: {lead_full['created_at']} UTC"
        )
        try:
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=text,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.exception("Failed to notify group: %s", e)

    # 2) Пишем в Google Sheet
    try:
        save_lead_to_sheet(lead_full)
    except Exception as e:
        logger.exception("Failed to save lead to sheet: %s", e)

    # 3) Пользователю — финальное сообщение
    await update.message.reply_text(
        "Заявка сформирована ✅ Я передал информацию менеджеру — он в курсе и скоро свяжется.\n"
        "Сейчас по вашим параметрам подберу и пришлю варианты.\n\n" + PROMO_TEXT,
        reply_markup=friendly_links_keyboard()
    )

    # Сбрасываем режим анкеты; помечаем, что анкета пройдена
    context.user_data["in_form"] = False
    context.user_data["form_completed"] = True
    return ConversationHandler.END

# ----------------- GPT: свободное общение -----------------
SYSTEM_PROMPT = (
    "Ты дружелюбный эксперт по Самуи и жилью на острове. Отвечай кратко и по делу, "
    "давай практику (климат, сезоны, ветра по пляжам, где штиль, районы, инфраструктура, "
    "логистика, школа/сад, серф/кайт и т.п.).\n\n"
    "Правила ретаргетинга:\n"
    "— Если вопрос ведёт к аренде/покупке/поиску объявлений, не упоминай чужие агентства. "
    "Вежливо направляй в ресурсы Cozy Asia: сайт, 2 телеграм-канала, Instagram и анкету /rent. "
    "— При любом упоминании подбора жилья добавляй короткий CTA: 'лучше заполнить /rent — подберу лоты и передам менеджеру'.\n"
)

async def gpt_reply(text: str, history: Optional[list] = None) -> str:
    if not oa_client:
        # фоллбек: локальный быстрый ответ + CTA
        return (
            "Я на связи. Могу помочь с погодой, ветрами по пляжам, районами и жильём.\n\n" + PROMO_TEXT
        )
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": text})

        resp = oa_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.5
        )
        out = resp.choices[0].message.content.strip()
        # Добавим короткий CTA мягко и не навязчиво
        return out + "\n\n" + "👉 Если речь о жилье: быстрее всего заполнить /rent — сделаю подборку и передам менеджеру."
    except Exception as e:
        logger.exception("OpenAI error: %s", e)
        return (
            "Я на связи. Могу ответить на любые вопросы. "
            "По недвижимости — жмите /rent или смотрите ссылки ниже.\n\n" + PROMO_TEXT
        )

# Любой текст вне анкеты → GPT
async def on_text(update: Update, context: CallbackContext):
    # если пользователь в анкете — игнорим и ждём ConversationHandler
    if context.user_data.get("in_form"):
        await update.message.reply_text("Упс, что-то пошло не так. Давай повторим вопрос?")
        return

    reply = await gpt_reply(update.message.text, history=None)
    await update.message.reply_text(reply, reply_markup=friendly_links_keyboard())

# ----------------- MAIN / WEBHOOK -----------------
def build_application() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_start)],
        states={
            Q_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_type)],
            Q_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_budget)],
            Q_AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_area)],
            Q_BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_bedrooms)],
            Q_CHECKIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_checkin)],
            Q_CHECKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_checkout)],
            Q_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_notes)],
        },
        fallbacks=[CommandHandler("cancel", on_cancel)],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("cancel", on_cancel))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

def main():
    app = build_application()

    if not BASE_URL:
        raise RuntimeError("ENV BASE_URL (или WEBHOOK_BASE) is required")

    base = BASE_URL.rstrip("/")
    path = WEBHOOK_PATH.rstrip("/")
    secret = TELEGRAM_TOKEN  # делаем путь уникальным
    full_url = f"{base}{path}/{secret}"

    # Установим вебхук и запустим встроенный веб-сервер PTB
    logger.info("==> Starting webhook on 0.0.0.0:%s | url=%s", PORT, full_url)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=full_url,
        secret_token=None,  # можно добавить секрет, если нужен
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
