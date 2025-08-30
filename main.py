# main.py
import os
import json
import logging
from datetime import datetime

from telegram import Update, __version__ as TG_VER
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ===================== CONFIG & LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cozyasia-bot")

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "").strip()
WEBHOOK_BASE     = os.environ.get("WEBHOOK_BASE", "").strip()
PORT             = int(os.environ.get("PORT", "10000"))
GROUP_CHAT_ID    = os.environ.get("GROUP_CHAT_ID", "").strip()     # например: -490897045931 (минус обязателен для супергруппы)
SHEET_ID         = os.environ.get("GOOGLE_SHEET_ID", "").strip()   # id таблицы
GOOGLE_CREDS_RAW = os.environ.get("GOOGLE_CREDS_JSON", "").strip() # мультистрочная JSON из Google Cloud

OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "").strip()    # опционально

if not TELEGRAM_TOKEN:
    raise RuntimeError("ENV TELEGRAM_TOKEN is required")
if not WEBHOOK_BASE or not WEBHOOK_BASE.startswith("http"):
    raise RuntimeError("ENV WEBHOOK_BASE must be your Render URL like https://xxx.onrender.com")

# ===================== SHEETS (gspread) =====================
# Ленивая инициализация клиента, чтобы не падать при старте, если переменные ещё не выставлены.
_gspread = None
_worksheet = None

def _init_sheets_once():
    """Подключаемся к Google Sheets один раз по требованию."""
    global _gspread, _worksheet
    if _worksheet is not None:
        return

    if not SHEET_ID or not GOOGLE_CREDS_RAW:
        log.warning("Google Sheets disabled (no GOOGLE_SHEET_ID or GOOGLE_CREDS_JSON)")
        return

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except Exception as e:
        log.error("gspread not installed or google auth missing: %s", e)
        return

    try:
        sa_info = json.loads(GOOGLE_CREDS_RAW)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
        _gspread = gspread.authorize(creds)
        sh = _gspread.open_by_key(SHEET_ID)
        try:
            _worksheet = sh.worksheet("Leads")
        except Exception:
            _worksheet = sh.sheet1  # fallback на первый лист, если нет "Leads"
        log.info("Google Sheets ready: %s", _worksheet.title)
    except Exception as e:
        log.error("Failed to init Google Sheets: %s", e)
        _worksheet = None

def append_lead_row(row_values: list):
    """Добавить строку в таблицу (если настроено)."""
    _init_sheets_once()
    if _worksheet is None:
        return False
    try:
        _worksheet.append_row(row_values, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        log.error("append_row failed: %s", e)
        return False

# ===================== STATE MACHINE /rent =====================
(
    Q_TYPE, Q_DISTRICT, Q_BUDGET, Q_BEDROOMS,
    Q_CHECKIN, Q_CHECKOUT, Q_NOTES
) = range(7)

RENT_INTRO = (
    "Запускаю короткую анкету. Вопрос 1/7:\n"
    "какой тип жилья интересует? (квартира/дом/вилла)\n\n"
    "Если хотите просто поговорить — задайте вопрос, я отвечу 🙂"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Что умеет этот бот?\n"
        "👋 Привет! Добро пожаловать в «Cozy Asia Real Estate Bot»\n\n"
        "😊 Я твой ИИ помощник и консультант.\n"
        "🗣️ Со мной можно говорить так же свободно, как с человеком.\n\n"
        "❓ Задавай вопросы:\n"
        "🏡 про дома, виллы и квартиры на Самуи\n"
        "🌴 про жизнь на острове, районы, атмосферу и погоду\n"
        "🥥 про быт, отдых и куда сходить на острове\n\n"
        "Чтобы оформить запрос на подбор жилья — напиши /rent"
    )
    await update.effective_message.reply_text(text)

async def cmd_rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text(RENT_INTRO)
    return Q_TYPE

async def q_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["type"] = update.message.text.strip()
    await update.message.reply_text("2/7: район (например: Ламай, Маенам, Чавенг)")
    return Q_DISTRICT

async def q_district(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["district"] = update.message.text.strip()
    await update.message.reply_text("3/7: бюджет на месяц (только число, например 50000)")
    return Q_BUDGET

async def q_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["budget"] = ''.join(ch for ch in update.message.text if ch.isdigit()) or update.message.text.strip()
    await update.message.reply_text("4/7: сколько спален нужно? (число)")
    return Q_BEDROOMS

async def q_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bedrooms"] = ''.join(ch for ch in update.message.text if ch.isdigit()) or update.message.text.strip()
    await update.message.reply_text("5/7: дата заезда (любой формат: 2025-12-01, 01.12.2025 и т. п.)")
    return Q_CHECKIN

async def q_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["checkin"] = update.message.text.strip()
    await update.message.reply_text("6/7: дата выезда (любой формат)")
    return Q_CHECKOUT

async def q_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["checkout"] = update.message.text.strip()
    await update.message.reply_text("7/7: важные условия/примечания (питомцы, бассейн, парковка и т.п.)")
    return Q_NOTES

async def q_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["notes"] = update.message.text.strip()

    ud = context.user_data
    summary = (
        "📝 Заявка сформирована и передана менеджеру.\n\n"
        f"Тип: {ud.get('type','')}\n"
        f"Район: {ud.get('district','')}\n"
        f"Спален: {ud.get('bedrooms','')}\n"
        f"Бюджет: {ud.get('budget','')}\n"
        f"Check-in: {ud.get('checkin','')}\n"
        f"Check-out: {ud.get('checkout','')}\n"
        f"Условия: {ud.get('notes','')}\n\n"
        "Сейчас подберу и пришлю подходящие варианты, а менеджер уже в курсе и свяжется при необходимости. "
        "Можно продолжать свободное общение — спрашивайте про районы, сезонность и т.д."
    )
    await update.message.reply_text(summary)

    # Уведомление в рабочую группу
    try:
        if GROUP_CHAT_ID:
            mention = (
                f"@{update.effective_user.username}"
                if update.effective_user and update.effective_user.username
                else f"ID: {update.effective_user.id if update.effective_user else '—'}"
            )
            group_text = (
                "🆕 Новая заявка Cozy Asia\n"
                f"Клиент: {mention}\n"
                f"Тип: {ud.get('type','')}\n"
                f"Район: {ud.get('district','')}\n"
                f"Бюджет: {ud.get('budget','')}\n"
                f"Спален: {ud.get('bedrooms','')}\n"
                f"Check-in: {ud.get('checkin','')}\n"
                f"Check-out: {ud.get('checkout','')}\n"
                f"Условия/прим.: {ud.get('notes','')}\n"
                f"Создано: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
            )
            await context.bot.send_message(chat_id=int(GROUP_CHAT_ID), text=group_text)
    except Exception as e:
        log.error("Failed to notify group: %s", e)

    # Запись в таблицу
    try:
        created = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        chat_id = update.effective_chat.id if update.effective_chat else ""
        username = update.effective_user.username if update.effective_user and update.effective_user.username else ""
        row = [
            created, str(chat_id), username,
            ud.get("district",""),
            ud.get("bedrooms",""),
            ud.get("budget",""),
            ud.get("checkin",""),
            ud.get("checkout",""),
            ud.get("type",""),
            ud.get("notes",""),
        ]
        ok = append_lead_row(row)
        if not ok:
            log.warning("Lead not saved to sheet (disabled or error).")
    except Exception as e:
        log.error("Sheet append error: %s", e)

    context.user_data.clear()
    return ConversationHandler.END

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("Окей, отменил анкету. Можем просто пообщаться или запустить /rent позже.")
    return ConversationHandler.END

# ===================== FREE CHAT =====================
async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Простая болталка. Если есть OPENAI_API_KEY — спрашиваем модель, иначе даём базовый ответ + предлагаем /rent."""
    text = update.message.text.strip()

    # Если пользователь сам пишет 'rent' — запускаем
    if text.lower() == "rent":
        return await cmd_rent(update, context)

    if OPENAI_API_KEY:
        try:
            # Лёгкий ответ через OpenAI (без тяжёлых зависимостей)
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            sys_prompt = (
                "Ты ассистент Cozy Asia (Самуи). Всегда дружелюбен. "
                "Если разговор касается аренды/покупки жилья — мягко предлагаешь пройти анкету командой /rent. "
                "Периодически напоминай о наших ресурсах:\n"
                "- Сайт: https://cozy.asia\n"
                "- Канал: https://t.me/cozy_asia\n"
                "- Правила/FAQ: https://t.me/cozy_asia_rules\n"
            )
            resp = client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[
                    {"role":"system","content":sys_prompt},
                    {"role":"user","content":text},
                ],
                temperature=0.6,
            )
            answer = resp.choices[0].message.content.strip()
            if "/rent" not in answer and any(k in text.lower() for k in ["снять", "аренда", "вилла", "дом", "квартира", "жильё", "жилье"]):
                answer += "\n\nЧтобы оформить запрос на подбор — напиши /rent."
            await update.message.reply_text(answer)
            return
        except Exception as e:
            log.error("OpenAI error: %s", e)

    # Фоллбэк без OpenAI
    fallback = (
        "Могу помочь с жильём, жизнью на Самуи, районами и т.д. "
        "Если готов(а) к подбору — напиши /rent и я задам 7 коротких вопросов.\n\n"
        "Наши ресурсы:\n"
        "- Сайт: https://cozy.asia\n"
        "- Канал: https://t.me/cozy_asia\n"
        "- Правила/FAQ: https://t.me/cozy_asia_rules"
    )
    await update.message.reply_text(fallback)

# ===================== BOOTSTRAP =====================
def build_application() -> Application:
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    rent_conv = ConversationHandler(
        entry_points=[CommandHandler("rent", cmd_rent)],
        states={
            Q_TYPE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, q_type)],
            Q_DISTRICT: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_district)],
            Q_BUDGET:   [MessageHandler(filters.TEXT & ~filters.COMMAND, q_budget)],
            Q_BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_bedrooms)],
            Q_CHECKIN:  [MessageHandler(filters.TEXT & ~filters.COMMAND, q_checkin)],
            Q_CHECKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_checkout)],
            Q_NOTES:    [MessageHandler(filters.TEXT & ~filters.COMMAND, q_notes)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(rent_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))
    return app

def run_webhook(app: Application):
    # ВАЖНО: url_path == тем же хвостом, который мы передаём в setWebhook,
    # чтобы встроенный web-сервер PTB не отвечал 404.
    url_path = f"webhook/{TELEGRAM_TOKEN}"
    webhook_url = f"{WEBHOOK_BASE.rstrip('/')}/{url_path}"
    log.info("==> start webhook on 0.0.0.0:%s | url=%s", PORT, webhook_url)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

def main():
    app = build_application()
    run_webhook(app)

if __name__ == "__main__":
    main()
