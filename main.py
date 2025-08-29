import os
import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

from dateutil import parser as dtparser
from dateutil.parser import ParserError

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, KeyboardButton,
    ReplyKeyboardMarkup
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
    ConversationHandler
)

# ====== ЛОГИ ================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("cozyasia-bot")

# ====== БЕЗОПАСНОЕ ЧТЕНИЕ ENV =============================================
def env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(f"ENV {name} is required")
    return val

# Обязательные / опциональные переменные
TELEGRAM_TOKEN   = env("TELEGRAM_TOKEN", required=True)
OPENAI_API_KEY   = env("OPENAI_API_KEY") or env("OPENAI_API_KEY_SECRET")
OPENAI_MODEL     = env("OPENAI_MODEL", "gpt-4o-mini")
GROUP_CHAT_ID    = env("GROUP_CHAT_ID")  # например: -4908974531
WEBHOOK_BASE     = env("WEBHOOK_BASE", required=True).rstrip("/")
WEBHOOK_PATH     = env("WEBHOOK_PATH", "/webhook")
PORT             = int(env("PORT", "10000"))

# Ресурсы для «маркетингового» блока
SITE_URL         = env("SITE_URL", "https://cozy.asia")
TG_CHANNEL_ALL   = env("TG_CHANNEL_ALL", "https://t.me/SamuiRental")
TG_CHANNEL_VILL  = env("TG_CHANNEL_VILL", "https://t.me/arenda_vill_samui")
INSTAGRAM_URL    = env("INSTAGRAM_URL", "https://www.instagram.com/cozy.asia")

# Google Sheets (опционально)
GS_DB_ID         = env("GOOGLE_SHEETS_DB_ID")
GS_CREDS_JSON    = env("GOOGLE_SERVICE_ACCOUNT_JSON")

# ====== OPENAI ==============================================================
openai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client ready.")
    except Exception as e:
        log.warning("OpenAI init failed: %s", e)

# ====== GOOGLE SHEETS (опционально) ========================================
gspread_client = None
worksheet = None
if GS_DB_ID and GS_CREDS_JSON:
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        sa_info = json.loads(GS_CREDS_JSON)
        creds = Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        gspread_client = gspread.authorize(creds)
        sh = gspread_client.open_by_key(GS_DB_ID)
        worksheet = sh.sheet1
        log.info("Google Sheets ready.")
    except Exception as e:
        log.warning("Google Sheets disabled: %s", e)
else:
    log.info("Google Sheets not configured; skipping.")

# ====== ВСПОМОГАТЕЛЬНОЕ ====================================================
def parse_any_date(text: str) -> Optional[str]:
    """
    Пытается распознать дату «в любых разумных форматах».
    Возвращает ISO 'YYYY-MM-DD' или None.
    """
    if not text:
        return None
    try:
        # dayfirst=True помогает для 01.12.2025 и т.п.
        dt = dtparser.parse(text, dayfirst=True, fuzzy=True)
        return dt.date().isoformat()
    except ParserError:
        return None

def marketing_block() -> Tuple[str, InlineKeyboardMarkup]:
    text = (
        "📌 Самый действенный способ — пройти короткую анкету командой /rent.\n"
        "Я сделаю подборку лотов по вашим критериям и передам менеджеру.\n\n"
        f"• Сайт: {SITE_URL}\n"
        f"• Канал с лотами: {TG_CHANNEL_ALL}\n"
        f"• Канал по виллам: {TG_CHANNEL_VILL}\n"
        f"• Instagram: {INSTAGRAM_URL}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Открыть сайт", url=SITE_URL)],
        [InlineKeyboardButton("📣 Телеграм-канал (все лоты)", url=TG_CHANNEL_ALL)],
        [InlineKeyboardButton("🏡 Канал по виллам", url=TG_CHANNEL_VILL)],
        [InlineKeyboardButton("📸 Instagram", url=INSTAGRAM_URL)],
    ])
    return text, kb

async def gpt_reply(user_text: str) -> str:
    """
    Свободное общение. Если OpenAI недоступен — даём локальный фолбэк.
    К ответу добавляем мягкую маршрутизацию к анкете/ресурсам.
    """
    base_answer = None

    if openai_client:
        try:
            sys_prompt = (
                "Ты дружелюбный русскоязычный помощник Cozy Asia по Самуи: климат, районы, быт, перелёты, визы, "
                "интересные места, а также базовые советы по подбору жилья. Не упоминай внешних агентств. "
                "Если вопрос идёт к конкретному подбору/аренде/покупке — мягко предложи пройти анкету /rent и дать ссылки Cozy Asia."
            )
            resp = openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_text}
                ],
                temperature=0.7,
                max_tokens=500
            )
            base_answer = resp.choices[0].message.content.strip()
        except Exception as e:
            log.warning("OpenAI error: %s", e)

    if not base_answer:
        # Простой фолбэк, чтобы бот не молчал
        base_answer = (
            "Постараюсь помочь. На Самуи — тропический климат: окт–дек чаще дожди и волна на востоке; "
            "янв–март суше и спокойнее; апрель — жаркий штиль; лето умеренное. "
            "Задайте вопрос конкретнее, и я уточню. "
        )

    # Добавим «мягкую воронку»
    extra, _ = marketing_block()
    return f"{base_answer}\n\n{extra}"

def safe_notify_group(context: ContextTypes.DEFAULT_TYPE, text: str):
    if not GROUP_CHAT_ID:
        return
    try:
        context.application.create_task(
            context.bot.send_message(chat_id=int(GROUP_CHAT_ID), text=text, parse_mode=ParseMode.HTML)
        )
    except Exception as e:
        log.warning("Notify group failed: %s", e)

def write_to_sheet(row: Dict[str, Any]):
    if not worksheet:
        return
    try:
        values = [
            row.get("time", ""),
            row.get("user_id", ""),
            row.get("username", ""),
            row.get("type", ""),
            row.get("area", ""),
            row.get("budget", ""),
            row.get("bedrooms", ""),
            row.get("checkin", ""),
            row.get("checkout", ""),
            row.get("notes", ""),
        ]
        worksheet.append_row(values)
    except Exception as e:
        log.warning("Sheet append failed: %s", e)

# ====== АНКЕТА /rent ========================================================
(
    RENT_TYPE,
    RENT_BUDGET,
    RENT_AREA,
    RENT_BEDS,
    RENT_CHECKIN,
    RENT_CHECKOUT,
    RENT_NOTES,
) = range(7)

def reset_form(user_data: Dict[str, Any]):
    user_data["rent_form"] = {
        "type": "",
        "budget": "",
        "area": "",
        "bedrooms": "",
        "checkin": "",
        "checkout": "",
        "notes": "",
        "submitted": False,  # антидубли
    }

async def rent_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_form(context.user_data)
    await update.message.reply_text(
        "Начнём подбор.\n1/7. Какой тип жилья интересует: квартира, дом или вилла?"
    )
    return RENT_TYPE

async def rent_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent_form"]["type"] = update.message.text.strip()
    await update.message.reply_text("2/7. Какой у вас бюджет в батах (месяц)?")
    return RENT_BUDGET

async def rent_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent_form"]["budget"] = update.message.text.strip()
    await update.message.reply_text("3/7. В каком районе Самуи предпочтительно жить?")
    return RENT_AREA

async def rent_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent_form"]["area"] = update.message.text.strip()
    await update.message.reply_text("4/7. Сколько нужно спален?")
    return RENT_BEDS

async def rent_beds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent_form"]["bedrooms"] = update.message.text.strip()
    await update.message.reply_text("5/7. Дата заезда (любым понятным форматом, например 01.12.2025)?")
    return RENT_CHECKIN

async def rent_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iso = parse_any_date(update.message.text)
    if not iso:
        await update.message.reply_text("Не распознал дату. Напишите ещё раз (например: 2025-12-01 или 01.12.2025).")
        return RENT_CHECKIN
    context.user_data["rent_form"]["checkin"] = iso
    await update.message.reply_text("6/7. Дата выезда (любой формат)?")
    return RENT_CHECKOUT

async def rent_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iso = parse_any_date(update.message.text)
    if not iso:
        await update.message.reply_text("Не распознал дату. Напишите ещё раз.")
        return RENT_CHECKOUT
    context.user_data["rent_form"]["checkout"] = iso
    await update.message.reply_text(
        "7/7. Важные условия? (близость к пляжу, с питомцами, парковка и т.п.)"
    )
    return RENT_NOTES

async def rent_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    form = context.user_data.get("rent_form", {})
    form["notes"] = update.message.text.strip()

    # Антидубли: если уже отправляли — не шлём повторно
    if not form.get("submitted"):
        form["submitted"] = True
        # Уведомим рабочую группу
        text = (
            "<b>🆕 Новая заявка Cozy Asia</b>\n"
            f"Клиент: @{update.effective_user.username or '—'} (ID: {update.effective_user.id})\n"
            f"Тип: {form['type']}\n"
            f"Район: {form['area']}\n"
            f"Бюджет: {form['budget']}\n"
            f"Спален: {form['bedrooms']}\n"
            f"Check-in: {form['checkin']}  |  Check-out: {form['checkout']}\n"
            f"Условия/прим.: {form['notes']}\n"
            f"Создано: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )
        safe_notify_group(context, text)

        # Запишем в Google Sheets, если есть
        write_to_sheet({
            "time": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            "user_id": str(update.effective_user.id),
            "username": update.effective_user.username or "",
            "type": form['type'],
            "area": form['area'],
            "budget": form['budget'],
            "bedrooms": form['bedrooms'],
            "checkin": form['checkin'],
            "checkout": form['checkout'],
            "notes": form['notes'],
        })

    # Ответ пользователю + авто-рекомендации
    extra_text, kb = marketing_block()
    await update.message.reply_text(
        "Заявка сформирована ✅\n"
        "Я уже передал детали менеджеру — он свяжется с вами в ближайшее время.\n"
        "Сейчас подберу варианты по вашей анкете и вышлю ссылки.\n\n"
        f"{extra_text}",
        reply_markup=kb
    )
    return ConversationHandler.END

async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Окей, если передумаете — пишите /rent.")
    return ConversationHandler.END

# ====== /start и свободное общение =========================================
def make_starter_keyboard() -> InlineKeyboardMarkup:
    _, kb = marketing_block()
    return kb

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "✅ Я уже тут!\n"
        "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n\n"
        "👉 Или нажмите команду /rent — задам несколько вопросов о жилье, сформирую заявку, предложу варианты и передам менеджеру."
    )
    await update.message.reply_text(text, reply_markup=make_starter_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Доступные команды: /start, /rent, /cancel")

async def free_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Любой текст, когда не идёт анкета — в GPT
    user_text = update.message.text.strip()
    answer = await gpt_reply(user_text)
    # Уточняющая reply-клавиатура (по желанию)
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("/rent")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await update.message.reply_text(answer, reply_markup=kb, parse_mode=ParseMode.HTML)

# ====== СБОРКА ПРИЛОЖЕНИЯ ==================================================
def build_application() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()

    # Анкета /rent
    rent_conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_start)],
        states={
            RENT_TYPE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_type)],
            RENT_BUDGET:   [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_budget)],
            RENT_AREA:     [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_area)],
            RENT_BEDS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_beds)],
            RENT_CHECKIN:  [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_checkin)],
            RENT_CHECKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_checkout)],
            RENT_NOTES:    [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_notes)],
        },
        fallbacks=[CommandHandler("cancel", rent_cancel)],
        allow_reentry=True,
        name="rent-conv",
        persistent=False,
    )

    app.add_handler(rent_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    # Свободное общение — в самом конце, чтобы не мешать /rent
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_chat))

    return app

# ====== ЗАПУСК WEBHOOK (Render) ============================================
def main():
    app = build_application()

    # URL, по которому Telegram будет слать апдейты:
    webhook_url = f"{WEBHOOK_BASE}{WEBHOOK_PATH}"

    log.info("Starting webhook on 0.0.0.0:%s, url=%s", PORT, webhook_url)

    # ВАЖНО: НЕ использовать asyncio.run(...) вокруг run_webhook,
    # иначе будет 'Cannot close a running event loop'.
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH.lstrip("/"),
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
