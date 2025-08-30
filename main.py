# -*- coding: utf-8 -*-
import os
import json
import time
import logging
from datetime import datetime
from typing import List

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cozyasia-bot")

# ===================== ENV =====================
# Telegram & Webhook
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
WEBHOOK_BASE   = os.environ.get("WEBHOOK_BASE", "").strip()   # https://<service>.onrender.com
PORT           = int(os.environ.get("PORT", "10000"))

# Group for notifications
GROUP_CHAT_ID  = os.environ.get("GROUP_CHAT_ID", "").strip()  # -100xxxxxxxxxx

# Google Sheets
SHEET_ID         = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDS_RAW = os.environ.get("GOOGLE_CREDS_JSON", "").strip()

# OpenAI
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_PROJECT = os.environ.get("OPENAI_PROJECT", "").strip()
OPENAI_ORG     = os.environ.get("OPENAI_ORG", "").strip()
OPENAI_MODEL   = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()

if not TELEGRAM_TOKEN:
    raise RuntimeError("ENV TELEGRAM_TOKEN is required")
if not WEBHOOK_BASE or not WEBHOOK_BASE.startswith("http"):
    raise RuntimeError("ENV WEBHOOK_BASE must be your Render URL like https://xxx.onrender.com")

# ===================== OpenAI helpers =====================
def _log_openai_env():
    if not OPENAI_API_KEY:
        log.warning("OpenAI disabled: no OPENAI_API_KEY")
        return
    try:
        import openai  # noqa
        key_type = "project-key" if OPENAI_API_KEY.startswith("sk-proj-") else "user-key"
        log.info("OpenAI ready | type=%s | model=%s | project=%s | org=%s",
                 key_type, OPENAI_MODEL, (OPENAI_PROJECT or "—"), (OPENAI_ORG or "—"))
        if OPENAI_API_KEY.startswith("sk-proj-") and not OPENAI_PROJECT:
            log.warning("You are using project-key but OPENAI_PROJECT is empty (proj_...).")
    except Exception as e:
        log.error("Failed to import openai: %s", e)

def _probe_openai():
    if not OPENAI_API_KEY:
        return
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=OPENAI_API_KEY,
            project=OPENAI_PROJECT or None,
            organization=OPENAI_ORG or None,
            timeout=30,
        )
        _ = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
        log.info("OpenAI probe OK.")
    except Exception as e:
        log.error("OpenAI probe failed: %s", e)

# ===================== GOOGLE SHEETS =====================
_gspread = None
_worksheet = None

def _init_sheets_once():
    """Ленивая инициализация Google Sheets (один раз)."""
    global _gspread, _worksheet
    if _worksheet is not None:
        return
    if not SHEET_ID or not GOOGLE_CREDS_RAW:
        log.warning("Google Sheets disabled (missing GOOGLE_SHEET_ID or GOOGLE_CREDS_JSON)")
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
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
            _worksheet = sh.sheet1
        # ensure headers
        headers = [
            "created_at", "chat_id", "username",
            "location", "bedrooms", "budget",
            "checkin", "checkout", "type", "notes"
        ]
        vals = _worksheet.get_all_values()
        if not vals:
            _worksheet.append_row(headers, value_input_option="RAW")
        else:
            head = vals[0]
            changed = False
            for h in headers:
                if h not in head:
                    head.append(h); changed = True
            if changed:
                _worksheet.update('A1', [head], value_input_option="RAW")
        log.info("Google Sheets ready: %s", _worksheet.title)
    except Exception as e:
        log.error("Failed to init Google Sheets: %s", e)
        _worksheet = None

def append_lead_row(row_values: List[str]) -> bool:
    _init_sheets_once()
    if _worksheet is None:
        return False
    try:
        _worksheet.append_row(row_values, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        log.error("append_row failed: %s", e)
        return False

# ===================== РЕСУРСЫ/ССЫЛКИ =====================
RESOURCES_HTML = (
    "<b>📎 Наши ресурсы</b>\n\n"
    "🌐 Web site — <a href='http://cozy-asiath.com/'>cozy-asiath.com</a>\n"
    "📣 Telegram — <a href='https://t.me/samuirental'>@samuirental</a>\n"
    "🏝️ Telegram — <a href='https://t.me/arenda_vill_samui'>@arenda_vill_samui</a>\n"
    "📸 Instagram — <a href='https://www.instagram.com/cozy.asia'>@cozy.asia</a>\n"
    "👤 Чат с менеджером — <a href='https://t.me/cozy_asia'>@cozy_asia</a>"
)

SHOW_LINKS_INTERVAL = 12 * 3600  # 12 часов

async def send_resources_ctx(message, context: ContextTypes.DEFAULT_TYPE, force: bool=False):
    now = time.time()
    last = context.user_data.get("links_last_ts", 0)
    if force or (now - last > SHOW_LINKS_INTERVAL):
        await message.reply_text(RESOURCES_HTML, parse_mode="HTML", disable_web_page_preview=True)
        context.user_data["links_last_ts"] = now

# ===================== ТЕКСТЫ =====================
START_GREETING = (
    "✅ Я уже тут!\n"
    "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n"
    "👉 Или нажмите команду /rent — я задам несколько вопросов о жилье, сформирую заявку, предложу варианты и передам менеджеру."
)

RENT_INTRO = (
    "Запускаю короткую анкету. Вопрос 1/7:\n"
    "какой тип жилья интересует? (квартира/дом/вилла)\n\n"
    "Если хотите просто поговорить — задайте вопрос, я отвечу 🙂"
)

# ===================== STATE MACHINE /rent =====================
(Q_TYPE, Q_DISTRICT, Q_BUDGET, Q_BEDROOMS, Q_CHECKIN, Q_CHECKOUT, Q_NOTES) = range(7)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(START_GREETING)

async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_resources_ctx(update.effective_message, context, force=True)

async def cmd_rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text(RENT_INTRO)
    return Q_TYPE

async def q_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["type"] = (update.message.text or "").strip()
    await update.message.reply_text("2/7: район (например: Ламай, Маенам, Чавенг)")
    return Q_DISTRICT

async def q_district(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["district"] = (update.message.text or "").strip()
    await update.message.reply_text("3/7: бюджет на месяц (только число, например 50000)")
    return Q_BUDGET

async def q_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    context.user_data["budget"] = "".join(ch for ch in txt if ch.isdigit()) or txt
    await update.message.reply_text("4/7: сколько спален нужно? (число)")
    return Q_BEDROOMS

async def q_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    context.user_data["bedrooms"] = "".join(ch for ch in txt if ch.isdigit()) or txt
    await update.message.reply_text("5/7: дата заезда (любой формат: 2025-12-01, 01.12.2025 и т. п.)")
    return Q_CHECKIN

async def q_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["checkin"] = (update.message.text or "").strip()
    await update.message.reply_text("6/7: дата выезда (любой формат)")
    return Q_CHECKOUT

async def q_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["checkout"] = (update.message.text or "").strip()
    await update.message.reply_text("7/7: важные условия/примечания (питомцы, бассейн, парковка и т.п.)")
    return Q_NOTES

async def q_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["notes"] = (update.message.text or "").strip()
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
        "Можно продолжать свободное общение — спрашивайте про районы, сезонность и т.д."
    )
    await update.message.reply_text(summary)

    # Уведомление в группу (если настроено)
    try:
        if GROUP_CHAT_ID:
            mention = (
                f"@{update.effective_user.username}"
                if (update.effective_user and update.effective_user.username)
                else f"(ID: {update.effective_user.id if update.effective_user else '—'})"
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
        username = update.effective_user.username if (update.effective_user and update.effective_user.username) else ""
        row = [
            created, str(chat_id), username,           # created_at, chat_id, username
            ud.get("district",""),                    # location
            ud.get("bedrooms",""),                    # bedrooms
            ud.get("budget",""),                      # budget
            ud.get("checkin",""),                     # checkin
            ud.get("checkout",""),                    # checkout
            ud.get("type",""),                        # type
            ud.get("notes",""),                       # notes
        ]
        ok = append_lead_row(row)
        if not ok:
            log.warning("Lead not saved to sheet (disabled or error).")
    except Exception as e:
        log.error("Sheet append error: %s", e)

    # Обязательная выдача «Наши ресурсы» после заявки
    await send_resources_ctx(update.message, context, force=True)

    context.user_data.clear()
    return ConversationHandler.END

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("Окей, отменил анкету. Можем просто пообщаться или запустить /rent позже.")
    return ConversationHandler.END

# ===================== FREE CHAT (GPT) =====================
async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Свободное общение. Ничего не перехватываем, мягко ведём к /rent."""
    text = (update.message.text or "").strip()

    if text.lower() == "rent":
        return await cmd_rent(update, context)

    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=OPENAI_API_KEY,
                project=OPENAI_PROJECT or None,
                organization=OPENAI_ORG or None,
                timeout=30,
            )
            sys_prompt = (
                "Ты ассистент Cozy Asia (Самуи). Дружелюбен, краток и полезен. "
                "Отвечай на вопросы о Самуи/аренде/жизни. Если уместно — предложи пройти анкету /rent."
            )
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": text},
                ],
                temperature=0.6,
            )
            answer = (resp.choices[0].message.content or "").strip()
            if "/rent" not in answer and any(
                k in text.lower() for k in ["снять", "аренда", "вилла", "дом", "квартира", "жильё", "жилье", "жилье"]
            ):
                answer += "\n\n👉 Чтобы оформить запрос на подбор — напиши /rent."
            await update.message.reply_text(answer)
            return
        except Exception as e:
            log.error("OpenAI chat error: %s", e)

    # Фоллбэк без OpenAI
    await update.message.reply_text(
        "Могу помочь с жильём, жизнью на Самуи, районами и т.д.\n\n👉 Чтобы оформить запрос на подбор — напиши /rent."
    )

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

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # ВАЖНО: сначала ConversationHandler для /rent,
    # затем ЕДИНСТВЕННЫЙ общий обработчик текста (GPT-чат).
    app.add_handler(rent_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))

    return app

def run_webhook(app: Application):
    """PTB 21.x: запуск вебхука."""
    url_path = f"webhook/{TELEGRAM_TOKEN}"
    webhook_url = f"{WEBHOOK_BASE.rstrip('/')}/{url_path}"
    log.info("==> start webhook on 0.0.0.0:%s | url=%s", PORT, webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        secret_token=None,
        url_path=url_path,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

def main():
    _log_openai_env()
    _probe_openai()
    app = build_application()
    run_webhook(app)

if __name__ == "__main__":
    main()
