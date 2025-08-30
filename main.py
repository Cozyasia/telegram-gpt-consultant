# main.py
import os
import json
import logging
from datetime import datetime
from typing import Optional

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

# ===================== CONFIG & LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cozyasia-bot")

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
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()     # sk-... или sk-proj-...
OPENAI_PROJECT = os.environ.get("OPENAI_PROJECT", "").strip()     # proj_..., если используешь project-key
OPENAI_ORG     = os.environ.get("OPENAI_ORG", "").strip()         # org_..., опционально
OPENAI_MODEL   = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()

if not TELEGRAM_TOKEN:
    raise RuntimeError("ENV TELEGRAM_TOKEN is required")
if not WEBHOOK_BASE or not WEBHOOK_BASE.startswith("http"):
    raise RuntimeError("ENV WEBHOOK_BASE must be your Render URL like https://xxx.onrender.com")

def _print_openai_cfg():
    if not OPENAI_API_KEY:
        log.warning("OpenAI disabled: no OPENAI_API_KEY")
        return
    subtype = "user-key"
    if OPENAI_API_KEY.startswith("sk-proj-"):
        subtype = "project-key"
    log.info(
        "OpenAI ready | type=%s | model=%s | project=%s | org=%s",
        subtype, OPENAI_MODEL, (OPENAI_PROJECT or "—"), (OPENAI_ORG or "—"),
    )

# ===================== GOOGLE SHEETS (ленивая инициализация) =====================
_gspread = None
_worksheet = None

def _init_sheets_once():
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
        log.error("gspread/google-auth not available: %s", e)
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
            _worksheet = sh.sheet1
        log.info("Google Sheets ready: %s", _worksheet.title)
    except Exception as e:
        log.error("Failed to init Google Sheets: %s", e)
        _worksheet = None

def append_lead_row(row_values: list) -> bool:
    _init_sheets_once()
    if _worksheet is None:
        return False
    try:
        _worksheet.append_row(row_values, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        log.error("append_row failed: %s", e)
        return False

# ===================== PROMO/TEXTS =====================
def promo_block() -> str:
    return (
        "📎 Наши ресурсы:\n"
        "🌐 Сайт — каталог и контакты\n"
        "https://cozy.asia\n\n"
        "📣 Канал — новости и подборки\n"
        "https://t.me/cozy_asia\n\n"
        "📘 Правила/FAQ — важные ответы\n"
        "https://t.me/cozy_asia_rules\n\n"
        "👉 Готовы к подбору жилья? Напишите /rent — задам 7 коротких вопросов и передам менеджеру."
    )

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
    await update.effective_message.reply_text(promo_block())

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

    try:
        created = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        chat_id = update.effective_chat.id if update.effective_chat else ""
        username = update.effective_user.username if (update.effective_user and update.effective_user.username) else ""
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

# ===================== OpenAI helpers =====================
def _get_openai_client():
    """
    Возвращает и кэширует клиент OpenAI или None (если ключа нет).
    """
    if not OPENAI_API_KEY:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=OPENAI_API_KEY,
            project=OPENAI_PROJECT or None,
            organization=OPENAI_ORG or None,
            timeout=60,           # общий таймаут
            max_retries=2,
        )
        return client
    except Exception as e:
        log.error("Failed to init OpenAI client: %s", e)
        return None

async def _probe_openai():
    """
    Маленькая проверка при старте, чтобы в логах сразу было понятно,
    работает ли ключ/проект/модель.
    """
    client = _get_openai_client()
    if not client:
        return
    try:
        # Запускаем синхронный вызов в пуле, чтобы не блокировать event loop
        import asyncio
        def _call():
            return client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": "pong"}],
                temperature=0.0,
            )
        resp = await asyncio.get_running_loop().run_in_executor(None, _call)
        txt = (resp.choices[0].message.content or "").strip()
        log.info("OpenAI probe OK: %s", txt[:120])
    except Exception as e:
        # Вытащим максимум деталей из исключения
        status = getattr(getattr(e, "response", None), "status_code", None)
        body   = None
        try:
            body = getattr(e, "response", None) and e.response.json()
        except Exception:
            pass
        log.error("OpenAI probe FAILED | status=%s | err=%s | body=%s", status, e, body)

# ===================== FREE CHAT =====================
def _looks_like_housing(text: str) -> bool:
    t = text.lower()
    keywords = ["снять", "аренда", "аренд", "вилла", "вилл", "дом", "квартира", "жильё", "жилье", "ренд"]
    return any(k in t for k in keywords)

async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text.lower() == "rent":
        return await cmd_rent(update, context)

    client = _get_openai_client()
    if client:
        import asyncio
        try:
            sys_prompt = (
                "Ты ассистент Cozy Asia (Самуи). Говоришь дружелюбно и по делу. "
                "Если разговор касается аренды/покупки жилья — мягко предложи /rent. "
                "В конце ответа всегда дай блок с ресурсами отдельным абзацем:\n\n"
                + promo_block()
            )

            def _call():
                return client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": text},
                    ],
                    temperature=0.6,
                )

            resp = await asyncio.get_running_loop().run_in_executor(None, _call)
            answer = (resp.choices[0].message.content or "").strip()

            if "/rent" not in answer and _looks_like_housing(text):
                answer += "\n\n👉 Чтобы оформить запрос на подбор — напиши /rent."

            await update.message.reply_text(answer)
            return
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            body   = None
            try:
                body = getattr(e, "response", None) and e.response.json()
            except Exception:
                pass
            log.error("OpenAI chat error | status=%s | err=%s | body=%s", status, e, body)

    # Фоллбэк без OpenAI
    fallback = "Могу помочь с жильём, жизнью на Самуи, районами и т.д.\n\n" + promo_block()
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
    _print_openai_cfg()
    app = build_application()
    # Проба OpenAI сразу после старта — смотри Render Logs
    import asyncio
    asyncio.get_event_loop().run_until_complete(_probe_openai())
    run_webhook(app)

if __name__ == "__main__":
    main()
