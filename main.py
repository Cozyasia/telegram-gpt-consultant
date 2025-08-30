import os
import logging
from datetime import datetime

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# =================== ЛОГИ ===================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("cozyasia-bot")

# =================== ENV ===================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
PORT = int(os.getenv("PORT", "10000"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("ENV TELEGRAM_TOKEN is required")
if not WEBHOOK_BASE.startswith("https://"):
    raise RuntimeError("ENV WEBHOOK_BASE must start with https://")

# =================== OpenAI ===================
try:
    from openai import OpenAI
    oai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception as e:
    oai = None
    log.warning("OpenAI SDK not available: %s", e)

SYSTEM_PROMPT = (
    "Ты — ИИ-помощник Cozy Asia (Самуи). Отвечай живо и по сути про Самуи: сезоны, районы, пляжи, ветра, быт. "
    "Если разговор уходит к аренде/покупке/вариантам — мягко предложи пройти анкету /rent и скажи, что менеджер свяжется. "
    "Не рекомендуй сторонние агентства. Пиши кратко."
)

# =================== Telegram Application ===================
application: Application = Application.builder().token(TELEGRAM_TOKEN).build()

# =================== Тексты ===================
WELCOME = (
    "✅ Я здесь!\n"
    "🌴 Можете спросить меня о пребывании на Самуи — подскажу и помогу.\n\n"
    "👉 Или нажмите команду /rent — задам несколько вопросов о жилье, "
    "сформирую заявку, предложу варианты и передам менеджеру.\n\n"
    "Также могу пообщаться в свободном режиме: погода, районы, пляжи, ветра и т. п."
)

# =================== АНКЕТА ===================
# Состояния (1..7)
TYPE, AREA, BEDROOMS, BUDGET, CHECKIN, CHECKOUT, NOTES = range(7)

def _reset_form(user_data: dict):
    user_data["rent_active"] = False
    user_data.pop("form", None)

async def rent_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Инициализация анкеты
    context.user_data["rent_active"] = True
    context.user_data["form"] = {}
    await update.effective_message.reply_text(
        "Запускаю короткую анкету. Вопрос 1/7: какой тип жилья интересует? (квартира/дом/вилла)\n"
        "Если хотите просто поговорить — задайте вопрос, я отвечу 🙂"
    )
    return TYPE

async def q_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["form"]["type"] = update.effective_message.text.strip()
    await update.effective_message.reply_text("2/7: В каком районе Самуи предпочитаете жить?")
    return AREA

async def q_area(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["form"]["area"] = update.effective_message.text.strip()
    await update.effective_message.reply_text("3/7: Сколько нужно спален?")
    return BEDROOMS

async def q_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["form"]["bedrooms"] = update.effective_message.text.strip()
    await update.effective_message.reply_text("4/7: Какой бюджет в батах в месяц?")
    return BUDGET

async def q_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["form"]["budget"] = update.effective_message.text.strip()
    await update.effective_message.reply_text("5/7: Дата заезда (любой формат: 2025-12-01, 01.12.2025 и т. п.)")
    return CHECKIN

def _parse_date(s: str) -> str:
    # Упрощённый парсинг без внешних зависимостей
    s = s.strip().replace("/", ".").replace("-", ".")
    parts = s.split(".")
    try:
        if len(parts) == 3:
            d, m, y = parts
            if len(y) == 2:
                y = "20" + y
            dt = datetime(int(y), int(m), int(d))
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    # если не распознали — вернём исходное
    return s

async def q_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["form"]["checkin"] = _parse_date(update.effective_message.text)
    await update.effective_message.reply_text("6/7: Дата выезда (любой формат)")
    return CHECKOUT

async def q_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["form"]["checkout"] = _parse_date(update.effective_message.text)
    await update.effective_message.reply_text("7/7: Важные условия? (питомцы, бассейн, парковка и т. п.)")
    return NOTES

async def q_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["form"]["notes"] = update.effective_message.text.strip()
    form = context.user_data.get("form", {})
    # Итог для клиента
    summary = (
        "📝 Заявка сформирована и передана менеджеру.\n\n"
        f"Тип: {form.get('type','-')}\n"
        f"Район: {form.get('area','-')}\n"
        f"Спален: {form.get('bedrooms','-')}\n"
        f"Бюджет: {form.get('budget','-')}\n"
        f"Check-in: {form.get('checkin','-')}\n"
        f"Check-out: {form.get('checkout','-')}\n"
        f"Условия: {form.get('notes','-')}\n\n"
        "Сейчас подберу и пришлю подходящие варианты, а менеджер уже в курсе и свяжется при необходимости. "
        "Можно продолжать свободное общение — спрашивайте про районы, сезонность и т.д."
    )
    await update.effective_message.reply_text(summary)

    # Тут можно уведомлять рабочую группу/Google Sheets и т.п.
    # (оставлено как заглушка)
    _reset_form(context.user_data)
    return ConversationHandler.END

async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _reset_form(context.user_data)
    await update.effective_message.reply_text("Ок, анкету остановил. Готов к свободному общению.")
    return ConversationHandler.END

# =================== GPT-ЧАТ ===================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME)

async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Если пользователь в анкете — GPT не вмешивается
    if context.user_data.get("rent_active"):
        return

    text = (update.effective_message.text or "").strip()
    if not text:
        return

    if not oai or not OPENAI_API_KEY:
        await update.effective_message.reply_text(
            "Я на связи и готов помочь! Могу рассказать про погоду, пляжи и районы. "
            "Для заявок по недвижимости — команда /rent."
        )
        return

    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.5,
        )
        answer = resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception("OpenAI error: %s", e)
        answer = (
            "Похоже, внешний ИИ отвечает дольше обычного. "
            "Спросите про Самуи — районы, сезонность, пляжи, ветра. "
            "А если нужен подбор жилья — жмите /rent, сформирую заявку."
        )

    await update.effective_message.reply_text(answer)

# =================== РЕГИСТРАЦИЯ ХЭНДЛЕРОВ ===================
# ConversationHandler ДОЛЖЕН СТОЯТЬ ПЕРЕД общим chat_handler,
# чтобы во время анкеты именно он принимал сообщения.
rent_conv = ConversationHandler(
    entry_points=[CommandHandler("rent", rent_entry)],
    states={
        TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_type)],
        AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_area)],
        BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_bedrooms)],
        BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_budget)],
        CHECKIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_checkin)],
        CHECKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_checkout)],
        NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_notes)],
    },
    fallbacks=[CommandHandler("cancel", rent_cancel)],
    name="rent_conversation",
    persistent=False,
)

application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(rent_conv)
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))

# =================== FASTAPI + WEBHOOK ===================
api = FastAPI(title="Cozy Asia Bot")

@api.get("/", response_class=PlainTextResponse)
async def health() -> str:
    return "OK"

@api.post(f"/webhook/{{token}}")
async def telegram_webhook(token: str, request: Request) -> Response:
    if token != TELEGRAM_TOKEN:
        return Response(status_code=403)

    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)

    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return Response(status_code=200)

async def setup_webhook():
    url = f"{WEBHOOK_BASE}/webhook/{TELEGRAM_TOKEN}"
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(url)
    log.info("Webhook set to %s", url)

@api.on_event("startup")
async def on_startup():
    await application.initialize()
    await application.start()
    await setup_webhook()
    log.info("Application started")

@api.on_event("shutdown")
async def on_shutdown():
    try:
        await application.stop()
        await application.shutdown()
    except Exception:
        pass
    log.info("Application stopped")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:api", host="0.0.0.0", port=PORT, log_level="info")
