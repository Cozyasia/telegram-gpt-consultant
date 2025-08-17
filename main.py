import os
import logging
import asyncio
from dotenv import load_dotenv

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)

# OpenAI SDK (v1+)
from openai import OpenAI

# ---- Setup ----
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

DEFAULT_SYSTEM_PROMPT = (
    os.getenv("SYSTEM_PROMPT") or
    open("system_prompt.txt", "r", encoding="utf-8").read().strip()
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("cozyasia-bot")

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
if not OPENAI_API_KEY:
    raise SystemExit("OPENAI_API_KEY is not set")

# OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# ---- OpenAI helper ----
async def ask_gpt(user_text: str, username: str | None = None) -> str:
    """Send user's text to OpenAI Chat Completions with a domain-specific system prompt.
    Uses a thread executor to avoid blocking the event loop.
    """
    messages = [
        {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
        {"role": "user", "content": user_text.strip()[:6000]},
    ]

    loop = asyncio.get_running_loop()
    try:
        # Run sync SDK call off the event loop
        completion = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                temperature=0.3,
                top_p=1.0,
                timeout=30,  # seconds
            )
        )
        answer = completion.choices[0].message.content or "⚠️ Пустой ответ от модели."
        return answer.strip()
    except Exception as e:
        logger.exception("OpenAI error")
        return f"⚠️ Ошибка обращения к модели: {e}"

# ---- Handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(
        "Привет! Я консультант Cozy Asia 🤖🏝️\n"
        "Задайте вопрос по аренде/покупке недвижимости на Самуи — отвечу и подскажу.",
        parse_mode=ParseMode.HTML
    )

async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.effective_message.text or ""
    reply = await ask_gpt(user_text, username=update.effective_user.username if update.effective_user else None)
    await update.effective_chat.send_message(reply, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Channel posts come as update.channel_post, but in PTB we filter by chat type CHANNEL
    user_text = update.effective_message.text or ""
    if not user_text:
        return
    reply = await ask_gpt(user_text, username=None)
    # Send answer back to the channel
    await update.effective_chat.send_message(reply, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error while processing update: %s", update)

def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))

    # Private chat text
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private_text))

    # Channel posts (bot must be an admin of the channel)
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, handle_channel_post))

    # (Optional) Handle groups by disabling privacy mode in BotFather
    # app.add_handler(MessageHandler((filters.ChatType.GROUPS & filters.TEXT), handle_private_text))

    app.add_error_handler(error_handler)

    logger.info("Bot started (polling)...")
    # Receive all update types so channel_post is guaranteed
    app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None, close_loop=False)

if __name__ == "__main__":
    main()
