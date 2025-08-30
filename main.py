import os
import json
import base64
import logging
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any

import dateparser
import gspread
from google.oauth2.service_account import Credentials

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, ConversationHandler, filters, CallbackContext
)

# ============== ЛОГИ ==============
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("cozyasia-bot")

# ============== ENV ==============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("ENV TELEGRAM_TOKEN is required")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").strip()
PORT = int(os.getenv("PORT", "10000"))

GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID") or os.getenv("GROUP_ID") or os.getenv("GROUP__CHAT_ID")
GROUP_CHAT_ID = int(GROUP_CHAT_ID) if GROUP_CHAT_ID else None

# Ресурсы (кнопки)
WEBSITE_URL = os.getenv("WEBSITE_URL", "https://cozy.asia")
TG_LOTS_URL = os.getenv("TG_LOTS_URL", "https://t.me/SamuiRental")
TG_VILLAS_URL = os.getenv("TG_VILLAS_URL", "https://t.me/arenda_vill_samui")
INSTAGRAM_URL = os.getenv("INSTAGRAM_URL", "https://www.instagram.com/cozy.asia")

# Google Sheets
G_SHEET_ID = os.getenv("GOOGLE_SHEETS_LEADS_ID", "").strip()
G_SHEET_TAB = os.getenv("GOOGLE_SHEETS_LEADS_TAB", "Leads")
G_SA_RAW = (
    os.getenv("GOOGLE_CREDS_JSON") or
    os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or
    ""
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def _parse_service_account(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    # Прямой JSON
    try:
        return json.loads(raw)
    except Exception:
        pass
    # base64
    try:
        dec = base64.b64decode(raw).decode("utf-8")
        return json.loads(dec)
    except Exception:
        pass
    # Одинарные кавычки -> двойные
    try:
        return json.loads(raw.replace("'", '"'))
    except Exception:
        return None

def _get_gclient():
    try:
        info = _parse_service_account(G_SA_RAW)
        if not info:
            raise RuntimeError("Service account JSON not parsed")
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        return gspread.authorize(creds)
    except Exception as e:
        logger.exception(f"[SHEETS] init error: {e}")
        return None

def write_lead_to_sheet(row: Dict[str, Any]) -> bool:
    if not G_SHEET_ID:
        logger.info("[SHEETS] skipped: no GOOGLE_SHEETS_LEADS_ID")
        return False
    gc = _get_gclient()
    if not gc:
        return False
    try:
        sh = gc.open_by_key(G_SHEET_ID)
        ws = sh.worksheet(G_SHEET_TAB)
    except Exception:
        try:
            ws = sh.add_worksheet(title=G_SHEET_TAB, rows="1000", cols="20")
        except Exception as e:
            logger.exception(f"[SHEETS] open sheet error: {e}")
            return False
    try:
        values = [
            row.get("created_at", ""),
            row.get("chat_id", ""),
            row.get("username", ""),
            row.get("location", ""),
            row.get("bedrooms", ""),
            row.get("budget", ""),
            row.get("people", ""),
            row.get("pets", ""),
            row.get("checkin", ""),
            row.get("checkout", ""),
            row.get("type", ""),
            row.get("notes", "")
        ]
        ws.append_row(values, value_input_option="USER_ENTERED")
        logger.info("[SHEETS] row appended")
        return True
    except Exception as e:
        logger.exception(f"[SHEETS] append error: {e}")
        return False

# ============== OPENAI (через HTTPX в SDK v1) ==============
# делаем лёгкую обёртку, чтобы не тянуть лишнее в обработчиках
class AIGateway:
    def __init__(self, api_key: str, model: str):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key) if api_key else None
        self.model = model

    async def ask(self, user_text: str, user_id: int) -> str:
        if not self.client:
            # если нет ключа — отвечаем локально простым текстом
            return ("Могу рассказать про Самуи: сезоны, районы, пляжи, где тише/ветер, "
                    "а также помочь с жильём. Для подбора нажмите /rent.\n\n"
                    "Наши ресурсы:\n• Сайт: {w}\n• Все лоты: {a}\n• Виллы: {b}\n• Instagram: {c}"
                    ).format(w=WEBSITE_URL, a=TG_LOTS_URL, b=TG_VILLAS_URL, c=INSTAGRAM_URL)

        system = (
            "Ты вежливый эксперт по Самуи (погода, сезоны, районы, пляжи, быт). "
            "Отвечай коротко и по делу, как живой консультант. "
            "Если вопрос явно про аренду/покупку/продажу недвижимости — мягко предложи пройти /rent "
            "(быстрый опрос на подбор), но НЕ прерывай свободный диалог. "
            "В конце иногда ненавязчиво добавляй полезные ссылки (сайт, каналы)."
        )
        try:
            resp = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text}
                ],
                temperature=0.5,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.exception(f"[OPENAI] error: {e}")
            fallback = ("Пока не могу спросить у модели. Но я всё равно могу помочь: "
                        "спросите про сезоны/районы/пляжи. Для подбора жилья — /rent.\n"
                        f"Сайт: {WEBSITE_URL}")
            return fallback

AI = AIGateway(OPENAI_API_KEY, OPENAI_MODEL)

# ============== UI ============
def resources_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("🌐 Открыть сайт", url=WEBSITE_URL)],
        [InlineKeyboardButton("📣 Телеграм-канал (все лоты)", url=TG_LOTS_URL)],
        [InlineKeyboardButton("🏡 Канал по виллам", url=TG_VILLAS_URL)],
        [InlineKeyboardButton("📷 Instagram", url=INSTAGRAM_URL)],
    ]
    return InlineKeyboardMarkup(kb)

WELCOME_TEXT = (
    "Что умеет этот бот?\n\n"
    "👋 Привет! Добро пожаловать в «Cosy Asia Real Estate Bot»\n\n"
    "😊 Я твой ИИ помощник и консультант.\n"
    "🗣️ Со мной можно говорить так же свободно, как с человеком.\n\n"
    "❓ Задавай вопросы:\n"
    "🏠 про дома, виллы и квартиры на Самуи\n"
    "🌴 про жизнь на острове, районы, атмосферу и погоду\n"
    "🍹 про быт, отдых и куда сходить на острове\n\n"
    "🛠 Самый действенный способ — пройти короткую анкету /rent.\n"
    "Я сделаю подборку лотов по вашим критериям и передам менеджеру."
)

# ============== АНКЕТА (Conversation) ==============
TYPE, LOCATION, BEDROOMS, BUDGET, CHECKIN, CHECKOUT, NOTES = range(7)

def _clean_int(text: str) -> Optional[int]:
    try:
        return int("".join(ch for ch in text if ch.isdigit()))
    except Exception:
        return None

def _parse_date(text: str) -> Optional[str]:
    if not text:
        return None
    dt = dateparser.parse(text, settings={"DATE_ORDER": "DMY"})
    return dt.strftime("%Y-%m-%d") if dt else None

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, reply_markup=resources_kb())

async def cmd_rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent"] = {}
    await update.message.reply_text(
        "Запускаю короткую анкету. Вопрос 1/7:\n"
        "Какой тип жилья интересует? (квартира/дом/вилла)\n"
        "Если хотите просто поговорить — задайте вопрос, я отвечу 🙂"
    )
    return TYPE

async def ask_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    context.user_data["rent"]["type"] = t
    await update.message.reply_text("2/7: В каком районе Самуи хотите жить? (например: Ламай, Маенам, Бопхут, Чавенг)")
    return LOCATION

async def ask_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = (update.message.text or "").strip()
    context.user_data["rent"]["location"] = loc
    await update.message.reply_text("3/7: Сколько спален нужно? (число)")
    return BEDROOMS

async def ask_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = _clean_int(update.message.text or "")
    context.user_data["rent"]["bedrooms"] = b or (update.message.text or "").strip()
    await update.message.reply_text("4/7: Какой у вас бюджет в батах (месяц)? (число)")
    return BUDGET

async def ask_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    budget = _clean_int(update.message.text or "")
    context.user_data["rent"]["budget"] = budget or (update.message.text or "").strip()
    await update.message.reply_text("5/7: Дата заезда (любой формат: 2025-12-01, 01.12.2025 и т. п.)")
    return CHECKIN

async def ask_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent"]["checkin"] = _parse_date(update.message.text or "") or (update.message.text or "").strip()
    await update.message.reply_text("6/7: Дата выезда (любой формат)")
    return CHECKOUT

async def ask_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent"]["checkout"] = _parse_date(update.message.text or "") or (update.message.text or "").strip()
    await update.message.reply_text("7/7: Важные условия/примечания (питомцы, бассейн, парковка, рабочее место и т. п.)")
    return NOTES

async def finish_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent"]["notes"] = (update.message.text or "").strip()

    data = context.user_data.get("rent", {}).copy()
    # Доп. поля
    data["created_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    data["chat_id"] = update.effective_chat.id
    data["username"] = (update.effective_user.username or "")
    data.setdefault("people", "")
    data.setdefault("pets", "")
    lead_pretty = (
        "📝 Заявка сформирована и передана менеджеру.\n\n"
        f"Тип: {data.get('type','')}\n"
        f"Район: {data.get('location','')}\n"
        f"Спален: {data.get('bedrooms','')}\n"
        f"Бюджет: {data.get('budget','')}\n"
        f"Check-in: {data.get('checkin','')}\n"
        f"Check-out: {data.get('checkout','')}\n"
        f"Условия: {data.get('notes','')}\n\n"
        "Сейчас подберу и пришлю подходящие варианты, а менеджер уже в курсе и свяжется при необходимости. "
        "Можно продолжать свободное общение — спрашивайте про районы, сезонность и т.д."
    )
    await update.message.reply_text(lead_pretty)

    # Уведомление в группу
    if GROUP_CHAT_ID:
        try:
            user_link = f"@{data['username']}" if data["username"] else f"(ID: {data['chat_id']})"
            text = (
                "🆕 Новая заявка Cozy Asia\n"
                f"Клиент: {user_link} (ID: {data['chat_id']})\n"
                f"Тип: {data.get('type','')}\n"
                f"Район: {data.get('location','')}\n"
                f"Бюджет: {data.get('budget','')}\n"
                f"Спален: {data.get('bedrooms','')}\n"
                f"Check-in: {data.get('checkin','')}\n"
                f"Check-out: {data.get('checkout','')}\n"
                f"Условия/прим.: {data.get('notes','')}\n"
                f"Создано: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
            )
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text)
        except Exception as e:
            logger.exception(f"[GROUP] notify error: {e}")

    # Запись в таблицу
    wrote = write_lead_to_sheet({
        "created_at": data.get("created_at",""),
        "chat_id": data.get("chat_id",""),
        "username": data.get("username",""),
        "location": data.get("location",""),
        "bedrooms": data.get("bedrooms",""),
        "budget": data.get("budget",""),
        "people": data.get("people",""),
        "pets": data.get("pets",""),
        "checkin": data.get("checkin",""),
        "checkout": data.get("checkout",""),
        "type": data.get("type",""),
        "notes": data.get("notes",""),
    })
    logger.info(f"[SHEETS] wrote={wrote}")

    # Сброс состояния анкеты — но оставляем свободный чат
    context.user_data.pop("rent", None)
    return ConversationHandler.END

async def cancel_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("rent", None)
    await update.message.reply_text("Окей, анкету закрыл. Можно продолжить свободное общение 🙂")
    return ConversationHandler.END

# ====== Свободный чат с ИИ ======
async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text or ""
    answer = await AI.ask(msg, update.effective_user.id)
    # Небольшая мягкая подсказка и ссылки не спамим каждый раз
    await update.message.reply_text(answer, reply_markup=resources_kb())

# ============== MAIN ==============
def build_application() -> Application:
    app: Application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Conversation должен стоять ПЕРЕД общим message handler
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", cmd_rent)],
        states={
            TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_location)],
            LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_bedrooms)],
            BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_budget)],
            BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_checkin)],
            CHECKIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_checkout)],
            CHECKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_notes)],
            NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_form)],
        },
        fallbacks=[CommandHandler("cancel", cancel_form)],
        name="rent_form",
        persistent=False,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)

    # общий чат — после conv, чтобы не перебивал анкету
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))

    return app

async def run_webhook(app: Application):
    if not WEBHOOK_BASE:
        # fallback: polling (на случай отладки)
        logger.warning("WEBHOOK_BASE not set — falling back to polling")
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await idle_forever()
        return

    webhook_url = f"{WEBHOOK_BASE.rstrip('/')}/webhook"
    logger.info(f"==> run_webhook port={PORT} url='{webhook_url}'")
    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

async def idle_forever():
    # аккуратная «заглушка», если вдруг polling
    while True:
        await asyncio.sleep(3600)

def main():
    app = build_application()
    try:
        asyncio.run(run_webhook(app))
    except RuntimeError as e:
        # защита от «Cannot close a running event loop» при горячем рестарте
        logger.warning(f"RuntimeError caught: {e}")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(run_webhook(app))

if __name__ == "__main__":
    main()
