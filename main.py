# -*- coding: utf-8 -*-
"""
Cozy Asia Bot — полностью обновлённый main.py
- Поддержка webhook (Render) и polling (локально).
- Анкетирование клиента по аренде: район → спальни → бюджет → пожелания → жильцы.
- Все ответы пишутся в Google Sheets (если настроено).
- Если вариантов нет — заявка уходит менеджеру.
"""

import os
import re
import json
import logging
from datetime import datetime
from typing import Dict, Any

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

# ====== ЛОГИ ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("cozy_bot")

# ====== ENV ======
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
PORT = int(os.environ.get("PORT", "10000"))

MANAGER_CHAT_ID = os.environ.get("MANAGER_CHAT_ID")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")

GREETING_MESSAGE = os.environ.get(
    "GREETING_MESSAGE",
    "Привет! Я ассистент Cozy Asia 🌴\n"
    "Напиши, что ищешь (район, бюджет, спальни, пожелания и т.д.) "
    "или нажми /rent — я помогу подобрать варианты."
)

# ====== Google Sheets ======
worksheet = None
sheet_ok = False
SHEET_COLUMNS = ["timestamp", "user_id", "area", "bedrooms", "budget",
                 "preferences", "tenants", "status"]

def setup_gsheets():
    global worksheet, sheet_ok
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDS_JSON:
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        sh = client.open_by_key(GOOGLE_SHEET_ID)
        if "Requests" in [ws.title for ws in sh.worksheets()]:
            worksheet = sh.worksheet("Requests")
        else:
            worksheet = sh.add_worksheet(title="Requests", rows="1000", cols="20")
            worksheet.append_row(SHEET_COLUMNS)
        sheet_ok = True
        log.info("Google Sheets подключен")
    except Exception as e:
        log.error("Ошибка подключения к Google Sheets: %s", e)

def save_request(row: Dict[str, Any]):
    if not sheet_ok or not worksheet:
        return
    try:
        values = [row.get(c, "") for c in SHEET_COLUMNS]
        worksheet.append_row(values, value_input_option="USER_ENTERED")
    except Exception as e:
        log.warning("Ошибка записи в таблицу: %s", e)

# ====== СТАДИИ ДИАЛОГА ======
AREA, BEDROOMS, BUDGET, PREFERENCES, TENANTS = range(5)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(GREETING_MESSAGE)

async def rent_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🗺 В каком районе Самуи хотите жить? (Маенам, Бопхут, Ламай...)")
    return AREA

async def rent_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["area"] = update.message.text.strip()
    await update.message.reply_text("🛏 Сколько спален нужно?")
    return BEDROOMS

async def rent_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = re.findall(r"\d+", update.message.text)
    context.user_data["bedrooms"] = txt[0] if txt else "1"
    await update.message.reply_text("💰 Какой бюджет в месяц (в батах)?")
    return BUDGET

async def rent_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = re.sub(r"\D", "", update.message.text)
    context.user_data["budget"] = txt or "0"
    await update.message.reply_text("✨ Есть ли особые пожелания? (например: бассейн, с питомцами, рядом с морем)")
    return PREFERENCES

async def rent_preferences(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["preferences"] = update.message.text.strip()
    await update.message.reply_text("👨‍👩‍👧 Сколько человек будет проживать?")
    return TENANTS

async def rent_tenants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tenants"] = update.message.text.strip()

    row = {
        "timestamp": datetime.utcnow().isoformat(),
        "user_id": update.effective_user.id,
        "area": context.user_data.get("area"),
        "bedrooms": context.user_data.get("bedrooms"),
        "budget": context.user_data.get("budget"),
        "preferences": context.user_data.get("preferences"),
        "tenants": context.user_data.get("tenants"),
        "status": "new"
    }
    save_request(row)

    msg = (f"📌 Заявка от @{update.effective_user.username or update.effective_user.id}\n"
           f"Район: {row['area']}\nСпальни: {row['bedrooms']}\n"
           f"Бюджет: {row['budget']} бат\n"
           f"Пожелания: {row['preferences']}\nЖильцы: {row['tenants']}")

    if MANAGER_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=int(MANAGER_CHAT_ID), text=msg)
        except Exception as e:
            log.warning("Не удалось отправить менеджеру: %s", e)

    await update.message.reply_text("Спасибо! 🙌 Я передал заявку менеджеру. Мы скоро свяжемся.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Опрос отменён. Введите /rent чтобы начать заново.")
    return ConversationHandler.END

# ====== СБОРКА ПРИЛОЖЕНИЯ ======
def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))

    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_start)],
        states={
            AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_area)],
            BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_bedrooms)],
            BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_budget)],
            PREFERENCES: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_preferences)],
            TENANTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_tenants)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    app.add_handler(conv)
    return app

# ====== MAIN ======
if __name__ == "__main__":
    setup_gsheets()
    app = build_app()

    if BASE_URL:  # Render → webhook
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH.lstrip("/"),
            webhook_url=f"{BASE_URL}{WEBHOOK_PATH}"
        )
    else:  # Локально → polling
        app.run_polling()
