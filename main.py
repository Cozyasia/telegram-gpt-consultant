import os, json, logging, datetime as dt
from typing import Dict, Any, Optional

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

import gspread
from dateutil import parser as dateparser
from openai import OpenAI

# ---------- ЛОГГЕР ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("cozyasia-bot")

# ---------- ENV ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_BASE  = os.environ.get("WEBHOOK_BASE")            # https://<render>.onrender.com
PORT          = int(os.environ.get("PORT", "10000"))

OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL     = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

GOOGLE_CREDS_JSON= os.environ["GOOGLE_CREDS_JSON"]        # многострочный JSON (как у тебя сейчас)
GOOGLE_SHEET_ID  = os.environ["GOOGLE_SHEET_ID"]          # ID таблицы (из URL)
SHEET_LEADS_NAME = os.environ.get("SHEET_LEADS_NAME", "Leads")

GROUP_CHAT_ID    = os.environ.get("GROUP_CHAT_ID")        # для уведомлений
BRAND_SITE       = os.environ.get("BRAND_SITE", "https://cozy.asia")
BRAND_CHAN_MAIN  = os.environ.get("BRAND_CHAN_MAIN", "https://t.me/cozy_asia")
BRAND_CHAN_RULES = os.environ.get("BRAND_CHAN_RULES", "https://t.me/cozy_asia_rules")

# ---------- CONSTANTS ----------
FREE_CHAT_PROMPT = f"""Ты — ИИ-помощник Cozy Asia Real Estate (Самуи).
Всегда дружелюбно и по-человечески. Если диалог касается жилья/районов/погоды,
обязательно ненавязчиво упоминай наши ресурсы:
• Сайт: {BRAND_SITE}
• Telegram: {BRAND_CHAN_MAIN}
• Правила/гайд: {BRAND_CHAN_RULES}
Если пользователь готов к подбору — предложи пройти короткую анкету командой /rent.
Не навязывай, но мягко подводи.
Короткие, понятные ответы. Русский язык по умолчанию."""

RENT_Q = [
    "1/7: какой тип жилья интересует? (квартира/дом/вилла)",
    "2/7: сколько человек?",
    "3/7: бюджет за месяц или за ночь? (цифра и валюта/мес/ночь)",
    "4/7: район на Самуи (или напишите «неважно»)",
    "5/7: дата заезда (любой формат: 2025-12-01, 01.12.2025 и т. п.)",
    "6/7: дата выезда (любой формат)",
    "7/7: важные условия/примечания (питомцы, бассейн, парковка и т.п.)",
]

RENT_FIELDS = ["type", "people", "budget", "location", "checkin", "checkout", "notes"]

# ---------- OPENAI ----------
oa_client = OpenAI(api_key=OPENAI_API_KEY)

def ai_answer(text: str) -> str:
    if not OPENAI_API_KEY:
        # Фоллбек без OpenAI
        return ("Я готов помочь и без ИИ 😊\n"
                f"Наш сайт: {BRAND_SITE}\nКанал: {BRAND_CHAN_MAIN}\n"
                "Чтобы подобрать жильё — напишите /rent.")
    try:
        resp = oa_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": FREE_CHAT_PROMPT},
                {"role": "user", "content": text}
            ],
            temperature=0.4,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception("OpenAI error")
        return ("Пока отвечу коротко, у меня заминка с ИИ. "
                f"Наш сайт: {BRAND_SITE}. Для подбора жилья — /rent.")

# ---------- SHEET ----------
def _gs_client():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    gc = gspread.service_account_from_dict(creds_dict, scopes=scopes)
    return gc

def append_lead(row: Dict[str, Any]) -> Optional[str]:
    """Пишем строку в лист Leads. Возвращаем текст ошибки или None."""
    try:
        gc = _gs_client()
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        ws = sh.worksheet(SHEET_LEADS_NAME)

        # порядок колонок: created_at, chat_id, username, location, bedrooms, budget, people, pets, checkin, checkout, notes
        ws.append_row([
            dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            str(row.get("chat_id","")),
            row.get("username","") or "",
            row.get("location","") or "",
            row.get("bedrooms","") or "",
            row.get("budget","") or "",
            row.get("people","") or "",
            row.get("pets","") or "",
            row.get("checkin","") or "",
            row.get("checkout","") or "",
            row.get("notes","") or ""
        ], value_input_option="USER_ENTERED")
        return None
    except Exception as e:
        log.exception("append_lead error")
        return str(e)

# ---------- HELPERS ----------
def parse_date(s: str) -> str:
    try:
        return dateparser.parse(s, dayfirst=True).date().isoformat()
    except Exception:
        return s.strip()

def notify_group(context: ContextTypes.DEFAULT_TYPE, lead: Dict[str, Any]) -> None:
    if not GROUP_CHAT_ID:
        return
    txt = (
        "🆕 **Новая заявка Cozy Asia**\n"
        f"Клиент: @{lead.get('username') or '—'} (ID: {lead.get('chat_id')})\n"
        f"Тип: {lead.get('type')}\n"
        f"Район: {lead.get('location')}\n"
        f"Бюджет: {lead.get('budget')}\n"
        f"Спален: {lead.get('bedrooms') or ''}\n"
        f"Check-in: {lead.get('checkin')}\n"
        f"Check-out: {lead.get('checkout')}\n"
        f"Условия/прим.: {lead.get('notes') or ''}\n"
        f"Создано: {dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )
    try:
        context.bot.send_message(chat_id=int(GROUP_CHAT_ID), text=txt, parse_mode="Markdown")
    except Exception:
        log.exception("notify_group error")

def reset_form(user_data: Dict[str, Any]):
    user_data["mode"] = "free"
    user_data["step"] = 0
    user_data["lead"] = {}

# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_form(context.user_data)
    hello = (
        "Что умеет этот бот?\n"
        "👋 Привет! Добро пожаловать в «Cozy Asia Real Estate Bot»\n\n"
        "😊 Я твой ИИ помощник и консультант.\n"
        "🗣 Со мной можно говорить так же свободно, как с человеком.\n\n"
        "❓ Задавай вопросы:\n"
        "🏡 про дома, виллы и квартиры на Самуи\n"
        "🌴 про жизнь на острове, районы, атмосферу и погоду\n"
        "🍹 про быт, отдых и куда сходить на острове\n\n"
        f"Чтобы оформить запрос на подбор жилья — напиши /rent\n"
        f"Наш сайт: {BRAND_SITE}\nКанал: {BRAND_CHAN_MAIN}"
    )
    await update.message.reply_text(hello)

async def rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "rent"
    context.user_data["step"] = 0
    context.user_data["lead"] = {
        "chat_id": update.effective_user.id,
        "username": update.effective_user.username or ""
    }
    await update.message.reply_text("Запускаю короткую анкету. " + RENT_Q[0])

async def free_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # если в режиме анкеты
    if context.user_data.get("mode") == "rent":
        idx = context.user_data.get("step", 0)

        # маппинг ответов
        key = RENT_FIELDS[idx]
        val = text

        if key in ("checkin", "checkout"):
            val = parse_date(text)

        context.user_data["lead"][key] = val

        idx += 1
        context.user_data["step"] = idx

        if idx < len(RENT_Q):
            await update.message.reply_text(RENT_Q[idx])
            return

        # анкета завершена => сохранить
        lead = context.user_data["lead"]
        # derive few fields for sheet
        lead["location"] = lead.get("location","")
        # bedrooms попытайся вытянуть из типа, но оставим пустым — вручную
        lead["bedrooms"] = ""
        # pets вытащим из notes при желании — сейчас не трогаем
        lead["pets"] = ""

        err = append_lead(lead)
        if err:
            log.error("Sheet append failed: %s", err)

        # уведомление в группу
        notify_group(context, lead)

        # клиенту подтверждение
        summary = (
            "📝 Заявка сформирована и передана менеджеру.\n\n"
            f"Тип: {lead.get('type')}\n"
            f"Район: {lead.get('location')}\n"
            f"Спален: {lead.get('bedrooms') or ''}\n"
            f"Бюджет: {lead.get('budget')}\n"
            f"Check-in: {lead.get('checkin')}\n"
            f"Check-out: {lead.get('checkout')}\n"
            f"Условия: {lead.get('notes') or ''}\n\n"
            "Сейчас подберу и пришлю подходящие варианты, а менеджер уже в курсе и свяжется при необходимости. "
            "Можно продолжать свободное общение — спрашивайте про районы, сезонность и т.д."
        )
        await update.message.reply_text(summary)
        reset_form(context.user_data)
        return

    # свободное общение (ИИ)
    reply = ai_answer(text)
    await update.message.reply_text(reply)

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Не понял команду. Для подбора жилья — /rent.")

# ---------- APP ----------
def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rent", rent))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_message))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    return app

def run_webhook(app: Application):
    # set webhook
    url = f"{WEBHOOK_BASE.rstrip('/')}/webhook/{TELEGRAM_TOKEN}"
    log.info("==> start webhook: %s", url)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=url,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    application = build_app()
    if WEBHOOK_BASE:
        run_webhook(application)
    else:
        log.info("==> start polling (WEBHOOK_BASE not set)")
        application.run_polling(drop_pending_updates=True)
