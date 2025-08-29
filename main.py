import os
import re
import json
import base64
import logging
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional

from dateutil import parser as dateparser

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters,
)

# --------- ЛОГИ ---------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cozyasia-bot")

# --------- КОНСТАНТЫ/ENV ---------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# ВАЖНО: указывайте БЕЗ дополнительного пути — только базовый https URL сервиса Render
# пример: https://telegram-gpt-consultant-d4yn.onrender.com
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")

MANAGER_CHAT_ID = int(os.getenv("MANAGER_CHAT_ID", "0") or 0)     # 5978240436
WORKGROUP_CHAT_ID = int(os.getenv("WORKGROUP_CHAT_ID", "0") or 0) # например: -1001234567890

GOOGLE_SHEETS_DB_ID = os.getenv("GOOGLE_SHEETS_DB_ID", "").strip()
GOOGLE_SERVICE_JSON_B64 = os.getenv("GOOGLE_SERVICE_JSON_B64", "").strip()

# Ссылки Cozy Asia
SITE_URL = "https://cozy.asia"
TG_CHANNEL_LOTS = "https://t.me/SamuiRental"
TG_CHANNEL_VILLAS = "https://t.me/arenda_vill_samui"
INSTAGRAM_URL = "https://www.instagram.com/cozy.asia?igsh=cmt1MHA0ZmM3OTRu"

# GPT параметры
GPT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GPT_SYSTEM = (
    "Ты — дружелюбный локальный ассистент Cozy Asia на Самуи. "
    "Отвечай кратко и по делу. Если вопрос касается климата, быта, районов, транспорта — отвечай сам. "
    "Если видишь явный интерес к недвижимости (аренда/покупка/продажа домов, вилл, квартир, лоты, цены), "
    "то дай полезный ответ по сути и добавь короткое приглашение пройти /rent у Cozy Asia (без упоминания сторонних агентств)."
)

# --------- GOOGLE SHEETS (опционально) ---------
_gspread = None
def _init_gspread():
    global _gspread
    if _gspread is not None:
        return _gspread
    if not (GOOGLE_SHEETS_DB_ID and GOOGLE_SERVICE_JSON_B64):
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_dict = json.loads(base64.b64decode(GOOGLE_SERVICE_JSON_B64).decode("utf-8"))
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        _gspread = client.open_by_key(GOOGLE_SHEETS_DB_ID).sheet1
        log.info("Google Sheets connected.")
        return _gspread
    except Exception as e:
        log.warning("Google Sheets init failed: %s", e)
        return None

def gs_append_row(values: List[Any]):
    sh = _init_gspread()
    if not sh:
        return
    try:
        sh.append_row(values, value_input_option="USER_ENTERED")
    except Exception as e:
        log.warning("GS append error: %s", e)

# --------- GPT (OpenAI) ---------
_client = None
def _get_openai_client():
    global _client
    if _client is None and OPENAI_API_KEY:
        from openai import OpenAI
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client

async def gpt_answer(prompt: str) -> str:
    """
    Асинхронно дергаем OpenAI. Если ключа нет или ошибка — возвращаем безопасный текст,
    чтобы бот не «падал» и продолжал объяснять пользователю, как получить помощь.
    """
    client = _get_openai_client()
    if not client:
        return ("Сейчас я могу отвечать на общие вопросы. "
                "По недвижимости — нажмите /rent или посмотрите ссылки ниже.")

    def _call():
        try:
            resp = client.chat.completions.create(
                model=GPT_MODEL,
                messages=[
                    {"role": "system", "content": GPT_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                max_tokens=600,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            log.warning("OpenAI error: %s", e)
            return ""

    text = await asyncio.to_thread(_call)
    if not text:
        text = ("У меня временно трудности с ИИ-ответом. "
                "По вопросам недвижимости — команда /rent или кнопки ниже.")
    return text

# --------- ПРОМО-БЛОК ---------
def promo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Открыть сайт", url=SITE_URL)],
        [InlineKeyboardButton("📣 Телеграм-канал (все лоты)", url=TG_CHANNEL_LOTS)],
        [InlineKeyboardButton("🏡 Канал по виллам", url=TG_CHANNEL_VILLAS)],
        [InlineKeyboardButton("📷 Instagram", url=INSTAGRAM_URL)],
    ])

PROMO_TEXT = (
    "📝 Самый действенный способ — пройти короткую анкету командой /rent.\n"
    "Я сделаю подборку лотов по вашим критериям и передам менеджеру."
)

REALTY_PATTERNS = re.compile(
    r"(дом|вил(ла|лы)|квартир|аренд|снять|сда(ч|ё)|купить|продать|лот|цена|бюджет|таунхаус)",
    re.IGNORECASE
)

# --------- АНКЕТА ---------
(
    ST_TYPE,
    ST_BUDGET,
    ST_AREA,
    ST_BEDS,
    ST_CHECKIN,
    ST_CHECKOUT,
    ST_NOTES,
) = range(7)

def _parse_date_any(s: str) -> Optional[str]:
    try:
        dt = dateparser.parse(s, dayfirst=True, yearfirst=False, fuzzy=True)
        if not dt:
            return None
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None

def _msg_application_card(user_mention: str, data: Dict[str, Any], lots_count: int) -> str:
    return (
        "🆕 Новая заявка Cozy Asia\n\n"
        f"Клиент: {user_mention}\n"
        f"Тип: {data.get('type','')}\n"
        f"Район: {data.get('area','')}\n"
        f"Бюджет: {data.get('budget','')}\n"
        f"Спален: {data.get('beds','')}\n"
        f"Заезд: {data.get('checkin','')}\n"
        f"Выезд: {data.get('checkout','')}\n"
        f"Условия/прим.: {data.get('notes','')}\n"
        f"Создано: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Подходящих лотов: {lots_count}\n"
        f"Каналы для просмотра: {TG_CHANNEL_LOTS} | {TG_CHANNEL_VILLAS}"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.set_my_commands([
        BotCommand("start", "Перезапустить"),
        BotCommand("rent", "Подобрать жильё"),
        BotCommand("cancel", "Отменить анкету"),
    ])
    text = (
        "✅ Я уже тут!\n"
        "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n\n"
        "👉 Или нажмите команду /rent — задам несколько вопросов о жилье, "
        "сформирую заявку, предложу варианты и передам менеджеру. "
        "Он свяжется с вами для уточнения."
    )
    await update.message.reply_text(text)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("form", None)
    context.user_data.pop("form_done", None)
    await update.message.reply_text("Окей, если передумаете — пишите /rent.")
    return ConversationHandler.END

# -- Анкета: шаги --
async def rent_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"] = {}
    return await rent_ask_type(update, context)

async def rent_ask_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("1/7. Какой тип жилья интересует: квартира, дом или вилла?")
    return ST_TYPE

async def rent_get_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["type"] = update.message.text.strip()
    await update.message.reply_text("2/7. Какой у вас бюджет в батах (месяц)?")
    return ST_BUDGET

async def rent_get_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["budget"] = update.message.text.strip()
    await update.message.reply_text("3/7. В каком районе Самуи предпочтительно жить?")
    return ST_AREA

async def rent_get_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["area"] = update.message.text.strip()
    await update.message.reply_text("4/7. Сколько нужно спален?")
    return ST_BEDS

async def rent_get_beds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["beds"] = update.message.text.strip()
    await update.message.reply_text("5/7. Дата заезда? Напишите в свободном формате (например, 1.12.2025).")
    return ST_CHECKIN

async def rent_get_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dt = _parse_date_any(update.message.text)
    if not dt:
        await update.message.reply_text("Не распознал дату. Напишите по-другому (например, 2025-12-01).")
        return ST_CHECKIN
    context.user_data["form"]["checkin"] = dt
    await update.message.reply_text("6/7. Дата выезда? (любой формат)")
    return ST_CHECKOUT

async def rent_get_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dt = _parse_date_any(update.message.text)
    if not dt:
        await update.message.reply_text("Не распознал дату. Напишите по-другому (например, 2026-01-15).")
        return ST_CHECKOUT
    context.user_data["form"]["checkout"] = dt
    await update.message.reply_text(
        "7/7. Важные условия? (близость к пляжу, с питомцами, парковка и т.п.)"
    )
    return ST_NOTES

async def rent_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["notes"] = update.message.text.strip()
    data = context.user_data["form"].copy()
    context.user_data["form_done"] = True  # флаг — анкету закончили

    # «Поиск лотов» (сейчас — заглушка; показываем 0 и даем ваши каналы)
    lots_count = 0

    # Уведомления менеджеру и в группу
    mention = f"@{update.effective_user.username}" if update.effective_user.username else f"{update.effective_user.full_name}"
    card = _msg_application_card(mention, data, lots_count)

    if MANAGER_CHAT_ID:
        try:
            await context.bot.send_message(MANAGER_CHAT_ID, card)
        except Exception as e:
            log.warning("Notify manager failed: %s", e)
    if WORKGROUP_CHAT_ID:
        try:
            await context.bot.send_message(WORKGROUP_CHAT_ID, card)
        except Exception as e:
            log.warning("Notify workgroup failed: %s", e)

    # Сохранить в Google Sheets (если подключено)
    try:
        gs_append_row([
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            update.effective_user.id,
            update.effective_user.username or "",
            data.get("type",""), data.get("area",""), data.get("budget",""),
            data.get("beds",""), data.get("checkin",""), data.get("checkout",""),
            data.get("notes",""),
        ])
    except Exception as e:
        log.warning("GS write failed: %s", e)

    # Ответ пользователю
    text = (
        "Готово! Ваша заявка сформирована и передана менеджеру. "
        "Скоро он свяжется с вами.\n\n"
        f"По вашим критериям сейчас нашёл {lots_count} подходящих лотов.\n"
        "Пока можно посмотреть публикации:\n"
        f"• Канал с лотами: {TG_CHANNEL_LOTS}\n"
        f"• Канал по виллам: {TG_CHANNEL_VILLAS}\n"
        f"• Instagram: {INSTAGRAM_URL}"
    )
    await update.message.reply_text(text, reply_markup=promo_keyboard())
    return ConversationHandler.END

# --------- СВОБОДНОЕ ОБЩЕНИЕ ---------
async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Если только что закончили анкету — просто чатимся (без повторной отправки заявки)
    # «Заявка» отправляется ТОЛЬКО внутри rent_finish.
    try:
        reply = await gpt_answer(text)
    except Exception as e:
        log.warning("free_text gpt error: %s", e)
        reply = "Я на связи. Готов помочь с любыми вопросами!"

    # Если есть признаки интереса к недвижимости — дополним промо-блоком (но чужих агентств не упоминаем)
    if REALTY_PATTERNS.search(text):
        reply += "\n\n" + PROMO_TEXT

        await update.message.reply_text(reply, reply_markup=promo_keyboard())
    else:
        await update.message.reply_text(reply)

# --------- СТАРТ ПРИЛОЖЕНИЯ ---------
async def setup_webhook(app: Application):
    """
    Настраиваем вебхук. В v21 сервер слушает ПУТЬ '/'.
    Поэтому webhook_url должен быть ровно WEBHOOK_BASE без дополнительных '/webhook'.
    """
    if not WEBHOOK_BASE:
        log.warning("WEBHOOK_BASE is empty -> fallback to polling.")
        return False

    try:
        # Сначала удалим на всякий случай
        await app.bot.delete_webhook(drop_pending_updates=True)
        await asyncio.sleep(0.2)

        webhook_url = WEBHOOK_BASE  # путь только '/', без /webhook
        await app.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
        log.info("Webhook set to: %s", webhook_url)
        return True
    except Exception as e:
        log.error("set_webhook failed: %s", e)
        return False

def build_application() -> Application:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("ENV TELEGRAM_TOKEN is required")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # /start, /cancel
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))

    # Анкета /rent
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_entry)],
        states={
            ST_TYPE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_get_type)],
            ST_BUDGET:  [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_get_budget)],
            ST_AREA:    [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_get_area)],
            ST_BEDS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_get_beds)],
            ST_CHECKIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_get_checkin)],
            ST_CHECKOUT:[MessageHandler(filters.TEXT & ~filters.COMMAND, rent_get_checkout)],
            ST_NOTES:   [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_finish)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    application.add_handler(conv)

    # Свободное общение (любой текст, который не перехватили ранее)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))

    return application

async def run():
    app = build_application()
    ok = await setup_webhook(app)

    if ok:
        # Запускаем HTTP-сервер PTB (слушает '/')
        port = int(os.getenv("PORT", "10000"))
        log.info("==> Starting webhook server on 0.0.0.0:%s | url=%s", port, WEBHOOK_BASE)
        await app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=WEBHOOK_BASE,
        )
    else:
        # Фоллбек — polling (на всякий случай)
        log.warning("Falling back to polling mode.")
        await app.run_polling(allowed_updates=Update.ALL_TYPES)

def main():
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass

if __name__ == "__main__":
    main()
