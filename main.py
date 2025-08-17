import os
import logging
from typing import Iterable
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- Настройки и переменные окружения ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("cozyasia-bot")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]  # обязателен
CHANNEL_ID = os.environ.get("CHANNEL_ID")  # '@имя_канала' или '-100...'
# Можно оставить пустым: тогда /post будет разрешён всем
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}

# ---------- Вспомогательные функции ----------
def is_admin(user_id: int) -> bool:
    """Если ADMIN_IDS пуст — разрешаем всем; иначе только перечисленным id."""
    return (not ADMIN_IDS) or (user_id in ADMIN_IDS)

def chunk_text(text: str, limit: int = 4096) -> Iterable[str]:
    """Режем длинные тексты под лимит Telegram."""
    for i in range(0, len(text), limit):
        yield text[i : i + limit]

# ---------- Хендлеры ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Бот запущен.\n"
        "Команды:\n"
        "• /post <текст> — отправит пост в канал (если задан CHANNEL_ID)\n"
        "• Отправь фото с подписью — улетит в канал как картинка\n"
        "• Просто напиши — я повторю сообщение",
    )

async def post_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка текста в канал из лички бота."""
    if not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Недостаточно прав.")
        return
    if not CHANNEL_ID:
        await update.message.reply_text("❗️CHANNEL_ID не задан в переменных окружения Render.")
        return

    # Текст берём из аргументов /post или из реплая
    text = " ".join(context.args).strip()
    if not text and update.message and update.message.reply_to_message:
        rep = update.message.reply_to_message
        text = (rep.text or rep.caption or "").strip()

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
    """Отправка фото с подписью из лички в канал."""
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        return
    if not CHANNEL_ID:
        await update.message.reply_text("❗️CHANNEL_ID не задан в переменных окружения Render.")
        return

    try:
        if update.message.photo:
            file_id = update.message.photo[-1].file_id  # самая большая
            caption = update.message.caption or ""
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            await update.message.reply_text("✅ Фото отправлено в канал.")
        elif update.message.document and update.message.document.mime_type.startswith("image/"):
            # Если кидают картинку как документ
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

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Простое эхо для теста диалога."""
    if update.message and update.message.text:
        await update.message.reply_text(update.message.text)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Произошла ошибка:", exc_info=context.error)

# ---------- Точка входа ----------
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # Команды — только из лички бота
    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("post", post_to_channel, filters=filters.ChatType.PRIVATE))

    # Фото/картинки из лички — в канал
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, photo_to_channel))
    app.add_handler(MessageHandler(filters.Document.IMAGE & filters.ChatType.PRIVATE, photo_to_channel))

    # Эхо для всех чатов (можно убрать, если не нужно)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    app.add_error_handler(error_handler)

    logger.info("🚀 Starting polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
