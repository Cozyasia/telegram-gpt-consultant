import os
import json
import logging
from datetime import datetime
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ====== ЛОГИ ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("cozyasia-bot")

# ====== ENV ======
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE", "").strip()     # https://<your>.onrender.com
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook").strip().lstrip("/")
PORT = int(os.environ.get("PORT", "10000"))

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()

GROUP_ID = os.environ.get("GROUP_ID")  # например: -4908974521

GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

# ====== ВАЛИДАЦИЯ КРИТИЧЕСКИХ ПЕРЕМЕННЫХ ======
if not TOKEN:
    raise RuntimeError("ENV TELEGRAM_TOKEN is required")
if not WEBHOOK_BASE.startswith("https://"):
    raise RuntimeError("ENV WEBHOOK_BASE must start with https://")

# ====== OpenAI (не валим бота при ошибке) ======
client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client initialised")
    except Exception as e:
        log.exception("OpenAI init failed: %s", e)
        client = None

# ====== Google Sheets (опционально) ======
gs = None
if GOOGLE_CREDS_JSON and GOOGLE_SHEET_ID:
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gs = gspread.authorize(credentials)
        log.info("Google Sheets client initialised")
    except Exception as e:
        log.exception("GSheets init failed: %s", e)
        gs = None

# ====== УТИЛЫ ======
def build_webhook_url() -> str:
    base = WEBHOOK_BASE.rstrip("/")
    path = WEBHOOK_PATH.strip("/")
    return f"{base}/{path}/{TOKEN}"

async def write_lead_row_safe(row: list[str]):
    if not gs or not GOOGLE_SHEET_ID:
        return
    try:
        sh = gs.open_by_key(GOOGLE_SHEET_ID)
        ws = sh.worksheet("Leads")
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.exception("Failed to append row to Google Sheet: %s", e)

async def gpt_answer(prompt: str) -> str:
    """Безопасный GPT-ответ с фирменным роутингом на ваши ресурсы."""
    advisory = (
        "\n\n— Самый действенный способ: пройди короткую анкету /rent. "
        "Я сделаю подборку лотов по критериям и передам менеджеру.\n"
        "Сайт: https://cozy.asia\n"
        "Канал (все лоты): https://t.me/SamuiRental\n"
        "Канал по виллам: https://t.me/arenda_vill_samui\n"
        "Instagram: https://www.instagram.com/cozy.asia/"
    )
    if not client:
        # Фолбэк, если OpenAI не настроен
        return ("Я на связи и готов ответить. "
                "Сейчас внешняя модель недоступна, но я всё равно помогу кратко. "
                "Задай вопрос про жизнь на Самуи, районы, сезон/погоду, ветра, пляжи и т.п."
                + advisory)

    system = (
        "Ты — дружелюбный ассистент Cozy Asia по Самуи. "
        "Отвечай по делу (климат, сезоны, ветра, районы, быт). "
        "Если вопрос касается аренды/покупки/где смотреть лоты — "
        "всегда направляй на ресурсы Cozy Asia (каналы/сайт/анкета /rent), "
        "но не блокируй свободную беседу."
    )

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt}
            ],
            temperature=0.4,
        )
        content = resp.choices[0].message.content.strip()
        return content + advisory
    except Exception as e:
        log.exception("OpenAI failed: %s", e)
        return ("Не удалось обратиться к модели. Но я на связи и помогу.\n"
                "Спроси про сезоны, ветра, районы, транспорт, быт." + advisory)

# ====== ХЕНДЛЕРЫ ======
WELCOME = (
    "Привет! Как я могу помочь тебе сегодня?\n\n"
    "🔧 Самый действенный способ — пройти короткую анкету /rent.\n"
    "Я сделаю подборку лотов (дома/апартаменты/виллы) по твоим критериям "
    "и передам менеджеру.\n\n"
    "• Сайт: https://cozy.asia\n"
    "• Канал с лотами: https://t.me/SamuiRental\n"
    "• Канал по виллам: https://t.me/arenda_vill_samui\n"
    "• Instagram: https://www.instagram.com/cozy.asia/\n\n"
    "А ещё можно просто поговорить — спрашивай про сезоны, погоду, ветра, районы 😊"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Мини-анкета: одна команда = запись в таблицу (пример)
    created = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    user = update.effective_user
    row = [
        created,
        str(update.effective_chat.id),
        (user.username or user.full_name or "—"),
        "",   # location
        "",   # bedrooms
        "",   # budget
        "",   # people
        "",   # pets
        "",   # checkin
        "",   # checkout
        "from /rent"
    ]
    await write_lead_row_safe(row)

    txt = ("Окей! Давай так: напиши в свободной форме, что важно (район, бюджет/мес, "
           "сколько спален, даты заезда/выезда, дети/питомцы, парковка и т.п.). "
           "Я зафиксирую и сразу передам менеджеру, а также соберу подборку из нашего канала.")
    await update.message.reply_text(txt)

async def any_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.message.text.strip()
    reply = await gpt_answer(q)
    await update.message.reply_text(reply)

async def errors(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Handler error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Упс, что-то пошло не так. Давай попробуем ещё раз?")
    except Exception:
        pass

# ====== APP ======
def build_application() -> Application:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rent", rent))

    # Любой текст -> GPT
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_text))

    app.add_error_handler(errors)
    return app

async def ensure_webhook(app: Application):
    desired = build_webhook_url()
    info = await app.bot.get_webhook_info()
    current = (info.url or "").strip()
    if current != desired:
        try:
            await app.bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass
        await app.bot.set_webhook(url=desired)
        log.info("Webhook set: %s", desired)
    else:
        log.info("Webhook already set: %s", desired)

def main():
    app = build_application()
    webhook_url = build_webhook_url()
    log.info("=> run_webhook port=%s url=%s", PORT, webhook_url)

    async def _startup(_):
        await ensure_webhook(app)
        log.info("Application started")

    app.post_init = _startup  # запускаем ensure_webhook после инициализации

    # HTTP сервер + webhook endpoint
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url,
        # path берётся из webhook_url, поэтому отдельный path не задаём
        drop_pending_updates=False,
    )

if __name__ == "__main__":
    main()
