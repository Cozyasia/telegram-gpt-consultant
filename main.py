import os
import logging
import asyncio

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==== ЛОГИ ====
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("cozyasia-bot")

# ==== ENV ====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
PORT = int(os.getenv("PORT", "10000"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("ENV TELEGRAM_TOKEN is required")
if not WEBHOOK_BASE.startswith("https://"):
    raise RuntimeError("ENV WEBHOOK_BASE must start with https://")

# ==== OpenAI ====
try:
    from openai import OpenAI
    oai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception as e:
    oai = None
    log.warning("OpenAI SDK not available: %s", e)

# ==== Telegram Application (PTB v21) ====
application: Application = Application.builder().token(TELEGRAM_TOKEN).build()

# --------- Handlers ---------
WELCOME = (
    "✅ Я здесь!\n"
    "🌴 Можете спросить меня о пребывании на Самуи — подскажу и помогу.\n\n"
    "👉 Или нажмите команду /rent — задам несколько вопросов о жилье, "
    "сформирую заявку, предложу варианты и передам менеджеру.\n\n"
    "Также могу пообщаться в свободном режиме: погода, районы, пляжи, ветра и т. п."
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME)

# Простой демонстрационный /rent — чтобы бот всегда отвечал.
# (Здесь только заглушка; твою анкету можешь подставить дальше.)
async def cmd_rent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Запускаю короткую анкету. Вопрос 1/7: какой тип жилья интересует? (квартира/дом/вилла)\n"
        "Если хотите просто поговорить — задайте вопрос, я отвечу 🙂"
    )

SYSTEM_PROMPT = (
    "Ты — ИИ-помощник Cozy Asia (Самуи). Отвечай живо и по сути. "
    "Если вопрос связан с арендой/покупкой/вариантами — мягко предложи пройти анкету /rent "
    "и укажи, что менеджер свяжется. Не советуй сторонние агентства."
)

async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    if not text:
        return

    # Если OpenAI недоступен — дай вежливый фолбэк, а бот не молчит.
    if not oai or not OPENAI_API_KEY:
        await update.effective_message.reply_text(
            "Я на связи и готов помочь! Могу рассказать про погоду, пляжи и районы. "
            "Для заявок по недвижимости — команда /rent."
        )
        return

    try:
        # OpenAI Responses API (SDK v1.x)
        resp = oai.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
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

# Регистрируем хэндлеры
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("rent", cmd_rent))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))

# ==== FastAPI + Webhook маршруты ====
api = FastAPI(title="Cozy Asia Bot")

@api.get("/", response_class=PlainTextResponse)
async def health() -> str:
    return "OK"

@api.post(f"/webhook/{{token}}")
async def telegram_webhook(token: str, request: Request) -> Response:
    # Принимаем апдейты ТОЛЬКО на точный токен
    if token != TELEGRAM_TOKEN:
        return Response(status_code=403)

    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)

    update = Update.de_json(data, application.bot)
    # Обрабатываем апдейт напрямую (без очереди) — надёжно и просто
    await application.process_update(update)
    return Response(status_code=200)

# ==== Жизненный цикл приложения ====
async def setup_webhook():
    url = f"{WEBHOOK_BASE}/webhook/{TELEGRAM_TOKEN}"
    # Сбрасываем старый вебхук и ставим новый
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(url)
    log.info("Webhook set to %s", url)

@api.on_event("startup")
async def on_startup():
    # Инициализация PTB
    await application.initialize()
    await application.start()
    await setup_webhook()
    log.info("Application started")

@api.on_event("shutdown")
async def on_shutdown():
    # Корректное выключение PTB
    try:
        await application.stop()
        await application.shutdown()
    except Exception:
        pass
    log.info("Application stopped")

# ==== Локальный запуск (не нужен на Render, но удобно для тестов) ====
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:api", host="0.0.0.0", port=PORT, log_level="info")
