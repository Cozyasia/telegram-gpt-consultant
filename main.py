import os
import json
import logging
from datetime import datetime

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

# ===== OpenAI =====
try:
    from openai import OpenAI
    OPENAI_CLIENT = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
except Exception:  # не валим весь бот, если ключа нет
    OPENAI_CLIENT = None
    OPENAI_MODEL = None

# ===== Google Sheets =====
import gspread
from google.oauth2 import service_account

# ===== Логирование =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cozyasia-bot")

# ===== Переменные окружения =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("ENV TELEGRAM_TOKEN is required")

# Render даёт RENDER_EXTERNAL_URL, но можно переопределить WEBHOOK_BASE вручную
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE") or os.getenv("RENDER_EXTERNAL_URL")
if not WEBHOOK_BASE:
    raise RuntimeError("ENV WEBHOOK_BASE or RENDER_EXTERNAL_URL is required")
WEBHOOK_BASE = WEBHOOK_BASE.rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook").strip()  # по твоим скринам путь именно /webhook
PORT = int(os.getenv("PORT", "10000"))

# Таблица/группа
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "").strip()
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID", "").strip()
LEADS_SHEET_NAME = os.getenv("LEADS_SHEET_NAME", "Leads")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "").strip()  # например "-490897045913" (число как строка ок)

# ===== Хелперы =====
def _gsheet_client():
    """Авторизуемся в Google по многострочному GOOGLE_CREDS_JSON."""
    if not GOOGLE_CREDS_JSON or not SPREADSHEET_ID:
        return None, "GOOGLE_CREDS_JSON or GOOGLE_SPREADSHEET_ID is empty"
    try:
        info = json.loads(GOOGLE_CREDS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SPREADSHEET_ID)
        ws = sh.worksheet(LEADS_SHEET_NAME)
        return ws, None
    except Exception as e:
        return None, f"Google auth/open error: {e}"

def _append_lead_row(row_values):
    ws, err = _gsheet_client()
    if err:
        log.warning("Sheets unavailable: %s", err)
        return False, err
    try:
        ws.append_row(row_values, value_input_option="USER_ENTERED")
        return True, None
    except Exception as e:
        log.warning("Sheets append error: %s", e)
        return False, str(e)

async def _notify_group(context: ContextTypes.DEFAULT_TYPE, text: str):
    if not GROUP_CHAT_ID:
        return
    try:
        await context.bot.send_message(
            chat_id=int(GROUP_CHAT_ID),
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("Group notify error: %s", e)

async def _ask_openai(prompt: str) -> str:
    if not OPENAI_CLIENT or not OPENAI_MODEL:
        # оффлайн-ответ, если ключа нет
        return "Я на связи! Пока что без доступа к OpenAI, но подскажу по базовым вопросам о Самуи."
    try:
        resp = OPENAI_CLIENT.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system",
                 "content": (
                     "Ты дружелюбный помощник Cozy Asia по Самуи. Отвечай кратко и по делу. "
                     "Не придумывай фактов, если не уверен — скажи об этом и предложи уточнить."
                 )},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.warning("OpenAI error: %s", e)
        return "Сервер ответа сейчас недоступен. Попробуем ещё раз через минуту."

# ===== Тексты =====
WELCOME = (
    "<b>Что умеет этот бот?</b>\n"
    "👋 Привет! Добро пожаловать в «Cozy Asia Real Estate Bot»\n\n"
    "😊 Я твой ИИ помощник и консультант.\n"
    "🗣 Со мной можно говорить так же свободно, как с человеком.\n\n"
    "❓ Задавай вопросы:\n"
    "🏡 про дома, виллы и квартиры на Самуи\n"
    "🌴 про жизнь на острове, районы, атмосферу и погоду\n"
    "🍹 про быт, отдых и куда сходить на острове\n\n"
    "Чтобы оформить запрос на подбор жилья — напиши /rent"
)

# ===== Анкета /rent =====
TYPE, AREA, BEDROOMS, BUDGET, CHECKIN, CHECKOUT, NOTES = range(7)

def _keyboard(options):
    return ReplyKeyboardMarkup([[o] for o in options], resize_keyboard=True, one_time_keyboard=True)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Окей, отменил. Можно продолжить свободное общение.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def rent_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent"] = {}
    await update.message.reply_text(
        "1/7: какой тип жилья интересует? (квартира/дом/вилла)\n\n"
        "Если хотите просто поговорить — задайте вопрос, я отвечу 🙂",
        reply_markup=_keyboard(["Квартира", "Дом", "Вилла"]),
    )
    return TYPE

async def rent_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent"]["type"] = update.message.text.strip()
    await update.message.reply_text("2/7: район? (например: Ламай, Маенам, Чавенг и т.п.)", reply_markup=ReplyKeyboardRemove())
    return AREA

async def rent_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent"]["area"] = update.message.text.strip()
    await update.message.reply_text("3/7: сколько спален нужно? (числом, например 2)")
    return BEDROOMS

async def rent_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent"]["bedrooms"] = update.message.text.strip()
    await update.message.reply_text("4/7: бюджет в батах? (числом, например 50000)")
    return BUDGET

async def rent_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent"]["budget"] = update.message.text.strip()
    await update.message.reply_text("5/7: дата заезда (любой формат: 2025-12-01, 01.12.2025 и т.п.)")
    return CHECKIN

# простенький парсер дат: отдаём YYYY-MM-DD, если не получилось — как ввёл пользователь
import dateparser

def _to_date(s: str) -> str:
    try:
        dt = dateparser.parse(s, settings={"DATE_ORDER": "DMY"})
        if dt:
            return dt.date().isoformat()
    except Exception:
        pass
    return s.strip()

async def rent_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent"]["checkin"] = _to_date(update.message.text)
    await update.message.reply_text("6/7: дата выезда (любой формат)")
    return CHECKOUT

async def rent_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent"]["checkout"] = _to_date(update.message.text)
    await update.message.reply_text("7/7: важные условия/примечания (питомцы, бассейн, парковка и т.п.)")
    return NOTES

async def rent_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent"]["notes"] = update.message.text.strip()
    data = context.user_data["rent"]
    user = update.effective_user

    # 1) Сообщение пользователю (короткое резюме)
    text_user = (
        "📝 <b>Заявка сформирована и передана менеджеру.</b>\n\n"
        f"Тип: {data.get('type')}\n"
        f"Район: {data.get('area')}\n"
        f"Спален: {data.get('bedrooms')}\n"
        f"Бюджет: {data.get('budget')}\n"
        f"Check-in: {data.get('checkin')}\n"
        f"Check-out: {data.get('checkout')}\n"
        f"Условия: {data.get('notes') or '—'}\n\n"
        "Сейчас подберу и пришлю подходящие варианты, а менеджер уже в курсе и свяжется при необходимости. "
        "Можно продолжать свободное общение — спрашивайте про районы, сезонность и т.д."
    )
    await update.message.reply_text(text_user, parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())

    # 2) Запись в Google Sheets
    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        created_at,
        str(update.effective_chat.id),
        (user.username or "—"),
        data.get("area") or "—",
        data.get("bedrooms") or "—",
        data.get("budget") or "—",
        "",  # people (в твоей таблице есть такой столбец — оставим пустым)
        "",  # pets
        data.get("checkin") or "—",
        data.get("checkout") or "—",
        data.get("type") or "—",
        data.get("notes") or "—",
    ]
    ok, err = _append_lead_row(row)
    if not ok:
        log.warning("Lead wasn't written to sheet: %s", err)

    # 3) Уведомление в рабочую группу
    uname = f"@{user.username}" if user.username else "—"
    group_text = (
        "<b>🆕 Новая заявка Cozy Asia</b>\n"
        f"Клиент: {uname} (ID: <code>{user.id}</code>)\n"
        f"Тип: {data.get('type')}\n"
        f"Район: {data.get('area')}\n"
        f"Бюджет: {data.get('budget')}\n"
        f"Спален: {data.get('bedrooms')}\n"
        f"Check-in: {data.get('checkin')}\n"
        f"Check-out: {data.get('checkout')}\n"
        f"Условия/прим.: {data.get('notes') or '—'}\n"
        f"Создано: {created_at} UTC"
    )
    await _notify_group(context, group_text)

    # очищаем и выходим в свободный чат
    context.user_data.pop("rent", None)
    return ConversationHandler.END

# ===== Свободный чат (вне анкеты) =====
async def free_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if not text.strip():
        return
    reply = await _ask_openai(text.strip())
    await update.message.reply_text(reply)

# ===== Сборка приложения =====
def build_application() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # /start /cancel
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Conversation /rent
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_entry)],
        states={
            TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_type)],
            AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_area)],
            BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_bedrooms)],
            BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_budget)],
            CHECKIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_checkin)],
            CHECKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_checkout)],
            NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_finish)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        per_chat=True,
        per_user=True,
        per_message=False,
    )
    app.add_handler(conv)

    # Свободный чат — добавляем ПОСЛЕ анкеты, чтобы она имела приоритет
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_chat))

    return app

def main():
    app = build_application()

    webhook_url = f"{WEBHOOK_BASE}{WEBHOOK_PATH}"
    log.info("==> run_webhook port=%s url=%s", PORT, webhook_url)

    # run_webhook сам поднимет aiohttp сервер, установит вебхук и будет крутиться
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url,
        url_path=WEBHOOK_PATH.lstrip("/"),  # чтобы точно ловить /webhook
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
