import os
from telegram.ext import Application, MessageHandler, filters, CommandHandler, ContextTypes
from telegram import Update
import openai
import logging

# Включаем логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Загружаем переменные
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Здравствуйте! Я виртуальный консультант. Задайте мне вопрос.")

# Обработка всех сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.message.chat_id

    # GPT-запрос
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "Ты — профессиональный агент по недвижимости на Самуи. Помогай клиенту, выявляй его потребности, общайся дружелюбно и конкретно."},
                {"role": "user", "content": user_message}
            ]
        )
        answer = response['choices'][0]['message']['content']
        await update.message.reply_text(answer)
    except Exception as e:
        await update.message.reply_text("Произошла ошибка. Попробуйте позже.")
        logging.error(f"Ошибка OpenAI: {e}")

# Запуск бота
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logging.info("Бот запущен.")
    application.run_polling()

if __name__ == "__main__":
    main()
