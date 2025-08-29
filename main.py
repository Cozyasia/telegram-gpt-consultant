import os
import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters, PicklePersistence
)

# ====== 3rd party for LLM + Sheets + dates ======
from openai import OpenAI
import gspread
from google.oauth2 import service_account
import dateparser

# ------------- Logging -------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("cozyasia-bot")

# ------------- ENV -------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("ENV TELEGRAM_TOKEN is required")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY_DEFAULT")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")  # ДОЛЖНО СОВПАДАТЬ с run_webhook
PORT = int(os.getenv("PORT", "10000"))

GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID") or os.getenv("GROUP_ID")  # отрицательное число для супергруппы

# Google Sheets (по желанию)
GSHEET_ID = os.getenv("GSPREAD_SHEET_ID") or os.getenv("GOOGLE_SHEET_ID")  # обязательна только если хотите писать в таблицу
GSHEET_TAB = os.getenv("GSPREAD_LEADS_SHEET", "Leads")
GOOGLE_SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")  # весь JSON сервис-аккаунта

# Ссылки на ваши ресурсы
LINK_SITE = os.getenv("LINK_SITE", "https://cozy.asia")
LINK_LISTINGS = os.getenv("LINK_LISTINGS", "https://t.me/SamuiRental")
LINK_VILLAS = os.getenv("LINK_VILLAS", "https://t.me/arenda_vill_samui")
LINK_IG = os.getenv("LINK_IG", "https://www.instagram.com/cozy.asia")

# ----------- States for Conversation -----------
(
    Q_TYPE,
    Q_BUDGET,
    Q_LOCATION,
    Q_BEDROOMS,
    Q_CHECKIN,
    Q_CHECKOUT,
    Q_NOTES,
) = range(7)

# ----------- Helpers -----------
def parse_date_any(s: str) -> Optional[str]:
    if not s:
        return None
    dt = dateparser.parse(
        s,
        settings={"PREFER_DATES_FROM": "future", "DATE_ORDER": "DMY"},
    )
    if not dt:
        return None
    return dt.date().isoformat()

def sheets_client():
    if not (GSHEET_ID and GOOGLE_SA_JSON):
        return None, None
    try:
        info = json.loads(GOOGLE_SA_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GSHEET_ID)
        ws = sh.worksheet(GSHEET_TAB)
        return gc, ws
    except Exception as e:
        log.warning("Sheets disabled: %s", e)
        return None, None

def promo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Открыть сайт", url=LINK_SITE)],
        [InlineKeyboardButton("📣 Телеграм-канал (все лоты)", url=LINK_LISTINGS)],
        [InlineKeyboardButton("🏡 Канал по виллам", url=LINK_VILLAS)],
        [InlineKeyboardButton("📷 Instagram", url=LINK_IG)],
    ])

PROMO_TEXT = (
    "🛠️ Самый действенный способ — пройти короткую анкету командой /rent.\n"
    "Я сделаю подборку лотов (дома/апартаменты/виллы) по вашим критериям и передам менеджеру."
)

# ----------- OpenAI -----------
oai_client = None
if OPENAI_API_KEY:
    oai_client = OpenAI(api_key=OPENAI_API_KEY)

async def ai_reply(user_text: str) -> str:
    """
    Свободный GPT-чат с мягкой регуляцией: отвечаем по существу
    и деликатно направляем к анкете/ресурсам.
    При отсутствии ключа даём оффлайн-ответ-заглушку.
    """
    if not oai_client:
        return (
            "Я на связи и могу помочь 🙂\n\n"
            "По недвижимости — нажмите /rent или смотрите ссылки ниже.\n\n" + PROMO_TEXT
        )
    try:
        sys = (
            "Ты дружелюбный ассистент Cozy Asia на Самуи. "
            "Отвечай по делу (погода, районы, быт, отдых). "
            "Когда вопрос уходит к аренде/покупке/продаже — "
            "не рекламируй сторонние агентства; мягко направляй к /rent "
            "и ресурсам Cozy Asia. Краткость, русская локаль."
        )
        resp = oai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": user_text},
            ],
            temperature=0.6,
        )
        content = resp.choices[0].message.content.strip()
        # Добавим короткую «подсказку» в конце, не перебивая свободный ответ:
        tail = "\n\n" + PROMO_TEXT
        return content + tail
    except Exception as e:
        log.warning("OpenAI error: %s", e)
        return (
            "Сейчас отвечу как смогу 🙂\n\n"
            + PROMO_TEXT
        )

# ----------- Conversation Handlers -----------
def reset_user_state(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("lead_submitted", None)
    context.user_data["form"] = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привет! Добро пожаловать в «Cozy Asia Real Estate Bot»\n\n"
        "😊 Я твой ИИ помощник и консультант. "
        "💬 Со мной можно говорить так же свободно, как с человеком.\n\n"
        "❓ Задавай вопросы:\n"
        "🏡 про дома, виллы и квартиры на Самуи\n"
        "🌴 про жизнь на острове, районы, атмосферу и погоду\n"
        "🍹 про быт, отдых и куда сходить на острове\n\n"
        + PROMO_TEXT
    )
    await update.message.reply_text(text, reply_markup=promo_keyboard())
    return ConversationHandler.END

async def rent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Не дублируем заявку: после успешной 7/7 — только свободный чат, пока явно не начнут /rent заново
    reset_user_state(context)
    await update.message.reply_text("Начнём подбор.\n1/7. Какой тип жилья интересует: квартира, дом или вилла?")
    return Q_TYPE

async def q_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["type"] = update.message.text.strip()
    await update.message.reply_text("2/7. Какой у вас бюджет в батах (месяц)?")
    return Q_BUDGET

async def q_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["budget"] = update.message.text.strip()
    await update.message.reply_text("3/7. В каком районе Самуи предпочтительно жить?")
    return Q_LOCATION

async def q_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["location"] = update.message.text.strip()
    await update.message.reply_text("4/7. Сколько нужно спален?")
    return Q_BEDROOMS

async def q_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["bedrooms"] = update.message.text.strip()
    await update.message.reply_text("5/7. Дата заезда? (любой формат даты)")
    return Q_CHECKIN

async def q_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    parsed = parse_date_any(raw)
    if not parsed:
        # принимаем любой формат — попробуем ещё раз
        await update.message.reply_text("Не распознал дату. Напишите любым понятным способом (пример: 1 окт, 2025-10-01, 01/10/25).")
        return Q_CHECKIN
    context.user_data["form"]["check_in"] = parsed
    await update.message.reply_text("6/7. Дата выезда? (любой формат даты)")
    return Q_CHECKOUT

async def q_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    parsed = parse_date_any(raw)
    if not parsed:
        await update.message.reply_text("Не распознал дату. Напишите ещё раз любым понятным способом 🙏")
        return Q_CHECKOUT
    context.user_data["form"]["check_out"] = parsed
    await update.message.reply_text("7/7. Важные условия? (близость к пляжу, с питомцами, парковка и т.п.)")
    return Q_NOTES

async def q_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["notes"] = update.message.text.strip()

    form = context.user_data["form"].copy()
    user = update.effective_user

    # ---- Write to Google Sheets (optional) ----
    gc, ws = sheets_client()
    if ws:
        try:
            ws.append_row([
                datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                str(update.effective_chat.id),
                (user.username or user.full_name or ""),
                form.get("location", ""),
                form.get("bedrooms", ""),
                form.get("budget", ""),
                "",  # people (если понадобится)
                "",  # pets (если понадобится)
                form.get("check_in", ""),
                form.get("check_out", ""),
                form.get("type", ""),
                form.get("notes", ""),
            ])
        except Exception as e:
            log.warning("Sheets append failed: %s", e)

    # ---- Notify group ----
    if GROUP_CHAT_ID:
        try:
            msg = (
                "🆕 Новая заявка Cozy Asia\n"
                f"Клиент: @{user.username or 'без_ника'} (ID: {user.id})\n"
                f"Тип: {form.get('type','')}\n"
                f"Район: {form.get('location','')}\n"
                f"Бюджет: {form.get('budget','')}\n"
                f"Спален: {form.get('bedrooms','')}\n"
                f"Check-in: {form.get('check_in','')}  |  Check-out: {form.get('check_out','')}\n"
                f"Условия/прим.: {form.get('notes','')}\n"
                f"Создано: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
            )
            await context.bot.send_message(chat_id=int(GROUP_CHAT_ID), text=msg)
        except Exception as e:
            log.warning("Group notify failed: %s", e)

    # ---- Tell user ----
    await update.message.reply_text(
        "Заявка сформирована ✅ Я передал параметры менеджеру — он в курсе и свяжется для уточнений.\n"
        "Параллельно подберу варианты и пришлю в этот чат. "
        "Если хотите продолжить свободный диалог — просто пишите. 🙂",
        reply_markup=promo_keyboard()
    )

    context.user_data["lead_submitted"] = True
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Окей, если передумаете — пишите /rent.")
    return ConversationHandler.END

# ----------- Free Chat -----------
async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Если анкета только что завершена — не создаём новую автоматически
    user_text = update.message.text.strip()
    reply = await ai_reply(user_text)
    await update.message.reply_text(reply, reply_markup=promo_keyboard())

# ----------- Build Application -----------
def build_application() -> Application:
    persistence = PicklePersistence(filepath="cozyasia.pickle")
    app: Application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .persistence(persistence)
        .concurrent_updates(True)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_cmd)],
        states={
            Q_TYPE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, q_type)],
            Q_BUDGET:  [MessageHandler(filters.TEXT & ~filters.COMMAND, q_budget)],
            Q_LOCATION:[MessageHandler(filters.TEXT & ~filters.COMMAND, q_location)],
            Q_BEDROOMS:[MessageHandler(filters.TEXT & ~filters.COMMAND, q_bedrooms)],
            Q_CHECKIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_checkin)],
            Q_CHECKOUT:[MessageHandler(filters.TEXT & ~filters.COMMAND, q_checkout)],
            Q_NOTES:   [MessageHandler(filters.TEXT & ~filters.COMMAND, q_notes)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="rent-form",
        persistent=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(conv)

    # Свободный чат — в самом конце
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))

    return app

# ----------- Main (Webhook) -----------
async def run() -> None:
    app = build_application()

    full_url = f"{WEBHOOK_BASE}{WEBHOOK_PATH}"
    log.info("=> run_webhook port=%s url=%s", PORT, full_url)

    # Очень важно: путь в set_webhook и run_webhook должен совпадать!
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.bot.set_webhook(url=full_url, allowed_updates=Update.ALL_TYPES)

    await app.initialize()
    await app.start()
    # В v21 run_webhook сам поднимет aiohttp сервер и пробросит запросы в приложение
    await app.updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH.lstrip("/"),
    )

    # держим процесс
    await asyncio.Event().wait()

def main():
    asyncio.run(run())

if __name__ == "__main__":
    main()
