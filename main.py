import os
import logging
import asyncio
from typing import Iterable, List, Dict

import requests
from telegram import Update
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- ЛОГИ ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("cozyasia-bot")

# ---------- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ----------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]                       # обязательно
CHANNEL_ID = os.environ.get("CHANNEL_ID")                      # '@имя_канала' или '-100...'
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")                   # обязателен для ответов ИИ
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "30"))

# ---------- СИСТЕМНЫЙ ПРОМПТ ДЛЯ ИИ ----------
SYSTEM_PROMPT = (
    "Ты — Cozy Asia Consultant, дружелюбный и чёткий помощник по аренде/покупке "
    "недвижимости на Самуи (Таиланд). Отвечай на русском. "
    "Дай по делу, кратко и полезно: стоимость, районы, расстояния до моря, сроки, депозиты, комиссии. "
    "Если данных не хватает — задай 1 уточняющий вопрос. "
    "Избегай воды, не придумывай фактов. Если спрашивают не по недвижимости — отвечай как обычный ассистент."
)

# ---------- ВСПОМОГАТЕЛЬНОЕ ----------
def is_admin(user_id: int) -> bool:
    # если ADMIN_IDS пуст — разрешим всем
    return (not ADMIN_IDS) or (user_id in ADMIN_IDS)

def chunk_text(text: str, limit: int = 4096):
    for i in range(0, len(text), limit):
        yield text[i:i+limit]

def _chat_completion(messages: List[Dict]) -> str:
    """Синхронный вызов Chat Completions API (через requests)."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 500,
    }
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=OPENAI_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip()

async def ai_answer(prompt: str) -> str:
    """Асинхронная обёртка поверх синхронного HTTP."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    return await asyncio.to_thread(_chat_completion, messages)

# ---------- ХЕНДЛЕРЫ /start, /post, фото-в-канал ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Бот запущен.\n"
        "Команды:\n"
        "• /post <текст> — отправит пост в канал (если задан CHANNEL_ID)\n"
        "• Отправь фото с подписью — улетит в канал как картинка\n"
        "• Просто напиши вопрос — отвечу как консультант Cozy Asia"
    )

async def post_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Недостаточно прав.")
        return
    if not CHANNEL_ID:
        await update.message.reply_text("❗️CHANNEL_ID не задан в переменных окружения Render.")
        return

    text = " ".join(context.args).strip()
    if not text and update.message and update.message.reply_to_message:
        src = update.message.reply_to_message
        text = (src.text or src.caption or "").strip()

    if not text:
        await update.message.reply_text("ℹ️ Использование: /post <текст> (или ответь /post на сообщение).")
        return

    sent = 0
    for chunk in chunk_text(text):
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        sent += 1
    await update.message.reply_text(f"✅ Отправлено в канал ({sent} сообщ.).")

async def photo_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    if not CHANNEL_ID or not update.message:
        return
    try:
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            caption = update.message.caption or ""
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            await update.message.reply_text("✅ Фото отправлено в канал.")
        elif update.message.document and update.message.document.mime_type and update.message.document.mime_type.startswith("image/"):
            file_id = update.message.document.file_id
            caption = update.message.caption or ""
            await context.bot.send_document(
                chat_id=CHANNEL_ID,
                document=file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            await update.message.reply_text("✅ Изображение (документ) отправлено в канал.")
    except Exception as e:
        logger.exception("Ошибка отправки фото в канал")
        await update.message.reply_text(f"❗️Ошибка отправки: {e}")

# ---------- ГЛАВНЫЙ ХЕНДЛЕР ТЕКСТА (ИИ-ОТВЕТ) ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user_text = update.message.text.strip()

    # индикация "печатает..."
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    if not OPENAI_API_KEY:
        await update.message.reply_text(
            "Чтобы я отвечал как ИИ-консультант, добавь переменную OPENAI_API_KEY в Render → Environment, "
            "а затем перезапусти сервис."
        )
        return

    try:
        reply = await ai_answer(user_text)
    except requests.exceptions.HTTPError as e:
        logger.exception("OpenAI HTTP error")
        code = getattr(e.response, "status_code", "?")
        await update.message.reply_text(f"❗️OpenAI HTTP error ({code}). Попробуй ещё раз.")
        return
    except Exception as e:
        logger.exception("OpenAI error")
        await update.message.reply_text(f"❗️Ошибка ИИ: {e}")
        return

    for chunk in chunk_text(reply):
        await update.message.reply_text(chunk)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Ошибка в обработчике:", exc_info=context.error)

# ---------- ТОЧКА ВХОДА ----------
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # Команды — только из лички
    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("post", post_to_channel, filters=filters.ChatType.PRIVATE))

    # Фото → канал (личка)
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, photo_to_channel))
    app.add_handler(MessageHandler(filters.Document.IMAGE & filters.ChatType.PRIVATE, photo_to_channel))

    # Текст → ответ ИИ (во всех чатах)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(error_handler)

    logger.info("🚀 Starting polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
