# main.py (safe & stable)
import os
import json
import logging
import datetime as dt
from typing import Dict, Any, Optional

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackContext, filters
)

# ---------- logging ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cozyasia")

# ---------- ENV ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BASE_URL       = (os.getenv("BASE_URL") or os.getenv("WEBHOOK_BASE") or "").rstrip("/")
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH", "/webhook").rstrip("/")
PORT           = int(os.getenv("PORT", "10000"))
GROUP_CHAT_ID  = int(os.getenv("GROUP_CHAT_ID", "0"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_PROJECT = os.getenv("OPENAI_PROJECT")

GS_JSON        = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")  # raw JSON
GS_SHEET_ID    = os.getenv("GOOGLE_SHEETS_DB_ID")
LEADS_SHEET    = os.getenv("GOOGLE_SHEETS_LEADS_SHEET", "Leads")

if not TELEGRAM_TOKEN:
    raise RuntimeError("ENV TELEGRAM_TOKEN is required")
if not BASE_URL:
    raise RuntimeError("ENV BASE_URL (or WEBHOOK_BASE) is required")

# ---------- OpenAI (лениво, с безопасностью) ----------
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

def get_oa():
    if not (OpenAI and OPENAI_API_KEY):
        return None
    try:
        return OpenAI(api_key=OPENAI_API_KEY, project=OPENAI_PROJECT) if OPENAI_PROJECT else OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        log.exception("OpenAI init failed: %s", e)
        return None

oa_client = get_oa()

# ---------- Dates ----------
try:
    import dateutil.parser as dparser
    _HAS_DATEUTIL = True
except Exception:
    _HAS_DATEUTIL = False

def parse_date_loose(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None
    if _HAS_DATEUTIL:
        try:
            d = dparser.parse(text, dayfirst=True, fuzzy=True)
            return d.strftime("%Y-%m-%d")
        except Exception:
            pass
    formats = [
        "%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y",
        "%m/%d/%Y", "%m-%d-%Y", "%Y.%m.%d", "%Y/%m/%d",
        "%d.%m.%y", "%d-%m-%y", "%Y%m%d"
    ]
    for f in formats:
        try:
            d = dt.datetime.strptime(text, f)
            return d.strftime("%Y-%m-%d")
        except Exception:
            continue
    return None

# ---------- Google Sheets (лениво и безопасно) ----------
_gsready = None
def _get_ws_safe():
    """Возвращает (ws, err_msg). Если что-то не так — (None, 'причина')."""
    global _gsready
    if _gsready is False:
        return None, "disabled"
    try:
        import gspread
    except Exception as e:
        _gsready = False
        return None, f"gspread import failed: {e}"

    if not GS_JSON or not GS_SHEET_ID:
        _gsready = False
        return None, "GS env missing"

    try:
        creds = json.loads(GS_JSON)
    except Exception as e:
        _gsready = False
        return None, f"bad JSON: {e}"

    try:
        gc = gspread.service_account_from_dict(creds)
        sh = gc.open_by_key(GS_SHEET_ID)
        try:
            ws = sh.worksheet(LEADS_SHEET)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=LEADS_SHEET, rows=2000, cols=20)
            ws.append_row([
                "created_at","chat_id","username",
                "location","bedrooms","budget",
                "people","pets",
                "check_in","check_out",
                "notes","type"
            ])
        _gsready = True
        return ws, None
    except Exception as e:
        _gsready = False
        return None, f"gspread init failed: {e}"

def save_lead_to_sheet_safe(lead: Dict[str, Any]):
    ws, err = _get_ws_safe()
    if not ws:
        log.warning("Sheets disabled -> %s", err)
        return
    try:
        headers = ws.row_values(1)
        need = ["created_at","chat_id","username","location","bedrooms","budget","people","pets","check_in","check_out","notes","type"]
        if not headers:
            ws.append_row(need)
            headers = need
        # Добавим отсутствующие колонки, если нужно
        missing = [h for h in need if h not in headers]
        if missing:
            ws.update([headers + missing], '1:1')
            headers = ws.row_values(1)

        row = []
        for h in headers:
            v = lead.get(h, "")
            if isinstance(v, (dt.date, dt.datetime)):
                v = v.strftime("%Y-%m-%d")
            row.append(v)
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.exception("Sheets append failed: %s", e)

# ---------- Общие тексты ----------
def links_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Открыть сайт", url="https://cozy.asia")],
        [InlineKeyboardButton("📣 Телеграм-канал (все лоты)", url="https://t.me/SamuiRental")],
        [InlineKeyboardButton("🏡 Канал по виллам", url="https://t.me/arenda_vill_samui")],
        [InlineKeyboardButton("📷 Instagram", url="https://www.instagram.com/cozy.asia")],
    ])

PROMO = (
    "🔧 Самый действенный способ — пройти короткую анкету /rent.\n"
    "Я сделаю подборку лотов по вашим критериям и передам менеджеру.\n\n"
    "• Сайт: https://cozy.asia\n"
    "• Канал с лотами: https://t.me/SamuiRental\n"
    "• Канал по виллам: https://t.me/arenda_vill_samui\n"
    "• Instagram: https://www.instagram.com/cozy.asia"
)

SYSTEM_PROMPT = (
    "Ты дружелюбный эксперт по Самуи и жилью. Давай практичные ответы (климат, сезоны, ветра по пляжам, районы, быт). "
    "Если разговор уходит к аренде/покупке, мягко направляй на ресурсы Cozy Asia и /rent. "
    "Не советуй сторонние агентства."
)

# ---------- GPT ----------
async def gpt_reply(text: str) -> str:
    if not oa_client:
        return "Я на связи: расскажу про погоду, ветра и районы. По жилью — лучше заполнить /rent.\n\n" + PROMO
    try:
        resp = oa_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role":"system","content":SYSTEM_PROMPT},
                {"role":"user","content":text}
            ],
            temperature=0.5
        )
        out = (resp.choices[0].message.content or "").strip()
        if not out:
            out = "Могу помочь с погодой, ветром, районами и жильём."
        return out + "\n\n👉 По жилью быстрее всего заполнить /rent — подберу лоты и передам менеджеру."
    except Exception as e:
        log.exception("OpenAI error: %s", e)
        return "Отвечу на любые вопросы. Если речь о жилье — жмите /rent или смотрите ссылки ниже.\n\n" + PROMO

# ---------- Анкета ----------
(Q_TYPE, Q_BUDGET, Q_AREA, Q_BEDS, Q_IN, Q_OUT, Q_NOTES) = range(7)

def hello_text():
    return (
        "✅ Я уже тут!\n"
        "🌴 Можете спросить меня о пребывании на Самуи — подскажу и помогу.\n\n"
        "👉 Или нажмите /rent — задам вопросы, сформирую заявку и передам менеджеру."
    )

async def cmd_start(update: Update, _: CallbackContext):
    await update.effective_chat.send_message(hello_text())
    await update.effective_chat.send_message(PROMO, reply_markup=links_kb())

async def cmd_ping(update: Update, _: CallbackContext):
    await update.message.reply_text("pong ✅")

async def cmd_debug(update: Update, _: CallbackContext):
    await update.message.reply_text(
        f"env: webhook={BASE_URL}{WEBHOOK_PATH}/<token> | openai={'on' if oa_client else 'off'} | sheets={'on' if _gsready else 'off or lazy'}"
    )

async def cmd_cancel(update: Update, context: CallbackContext):
    context.user_data.clear()
    await update.message.reply_text("Окей, если передумаете — /rent.")

async def rent_start(update: Update, context: CallbackContext):
    ud = context.user_data
    ud.clear()
    ud["lead"] = {}
    ud["in_form"] = True
    await update.message.reply_text("1/7. Какой тип жилья интересует: квартира, дом или вилла?")
    return Q_TYPE

async def q_type(update: Update, context: CallbackContext):
    context.user_data["lead"]["type"] = update.message.text.strip().title()
    await update.message.reply_text("2/7. Какой бюджет в батах (месяц)?")
    return Q_BUDGET

async def q_budget(update: Update, context: CallbackContext):
    val = "".join(ch for ch in update.message.text if ch.isdigit()) or update.message.text
    context.user_data["lead"]["budget"] = val
    await update.message.reply_text("3/7. В каком районе Самуи предпочтительно жить?")
    return Q_AREA

async def q_area(update: Update, context: CallbackContext):
    context.user_data["lead"]["location"] = update.message.text.strip().title()
    await update.message.reply_text("4/7. Сколько нужно спален?")
    return Q_BEDS

async def q_beds(update: Update, context: CallbackContext):
    val = "".join(ch for ch in update.message.text if ch.isdigit()) or update.message.text
    context.user_data["lead"]["bedrooms"] = val
    await update.message.reply_text("5/7. Дата заезда? (можно в любом формате)")
    return Q_IN

async def q_in(update: Update, context: CallbackContext):
    d = parse_date_loose(update.message.text)
    if not d:
        await update.message.reply_text("Не понял дату. Попробуйте, например: 01.12.2025 или 2025-12-01.")
        return Q_IN
    context.user_data["lead"]["check_in"] = d
    await update.message.reply_text("6/7. Дата выезда?")
    return Q_OUT

async def q_out(update: Update, context: CallbackContext):
    d = parse_date_loose(update.message.text)
    if not d:
        await update.message.reply_text("Не понял дату. Попробуйте, например: 01.01.2026 или 2026-01-01.")
        return Q_OUT
    context.user_data["lead"]["check_out"] = d
    await update.message.reply_text("7/7. Важные условия? (пляж, питомцы, парковка и т.п.)")
    return Q_NOTES

async def q_notes(update: Update, context: CallbackContext):
    lead = context.user_data["lead"]
    lead["notes"] = update.message.text.strip()

    user = update.effective_user
    lead_full = {
        "created_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "chat_id": str(user.id),
        "username": user.username or "",
        "location": lead.get("location",""),
        "bedrooms": lead.get("bedrooms",""),
        "budget": lead.get("budget",""),
        "people": "",
        "pets": "",
        "check_in": lead.get("check_in",""),
        "check_out": lead.get("check_out",""),
        "notes": lead.get("notes",""),
        "type": lead.get("type",""),
    }

    # уведомление в рабочую группу
    if GROUP_CHAT_ID != 0:
        text = (
            "🆕 *Новая заявка Cozy Asia*\n"
            f"Клиент: @{user.username or '—'} (ID: {user.id})\n"
            f"Тип: {lead_full['type'] or '—'}\n"
            f"Район: {lead_full['location'] or '—'}\n"
            f"Бюджет: {lead_full['budget'] or '—'}\n"
            f"Спален: {lead_full['bedrooms'] or '—'}\n"
            f"Check-in: {lead_full['check_in'] or '—'} | Check-out: {lead_full['check_out'] or '—'}\n"
            f"Условия/прим.: {lead_full['notes'] or '—'}\n"
            f"Создано: {lead_full['created_at']} UTC"
        )
        try:
            await context.bot.send_message(GROUP_CHAT_ID, text, parse_mode="Markdown")
        except Exception as e:
            log.exception("Group notify failed: %s", e)

    # запись в таблицу (не роняет бота, если что-то не так)
    try:
        save_lead_to_sheet_safe(lead_full)
    except Exception as e:
        log.exception("Sheet save outer failed: %s", e)

    await update.message.reply_text(
        "Заявка сформирована ✅ Менеджер в курсе и свяжется.\n"
        "Сейчас по вашим параметрам подберу варианты.\n\n" + PROMO,
        reply_markup=links_kb()
    )

    context.user_data.clear()
    context.user_data["form_completed"] = True
    return ConversationHandler.END

# свободный чат
async def on_text(update: Update, context: CallbackContext):
    if context.user_data.get("in_form"):
        await update.message.reply_text("Упс, давай закончим вопрос. Или /cancel.")
        return
    reply = await gpt_reply(update.message.text)
    await update.message.reply_text(reply, reply_markup=links_kb())

# ---------- app ----------
def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_start)],
        states={
            Q_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_type)],
            Q_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_budget)],
            Q_AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_area)],
            Q_BEDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_beds)],
            Q_IN: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_in)],
            Q_OUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_out)],
            Q_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_notes)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

def main():
    app = build_app()
    full_url = f"{BASE_URL}{WEBHOOK_PATH}/{TELEGRAM_TOKEN}"
    log.info("==> run_webhook port=%s url=%s", PORT, full_url)
    app.run_webhook(
        listen="0.0.0.0", port=PORT,
        webhook_url=full_url,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
