import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from telegram import (Update, InlineKeyboardMarkup, InlineKeyboardButton)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

# === ЛОГИ ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("cozyasia-bot")

# === КОНФИГ / ENV ===
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

MANAGER_CHAT_ID = int(os.getenv("MANAGER_CHAT_ID", "5978240436"))  # Cozy Asia manager
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))  # <- сюда поставьте ID рабочей группы

GOOGLE_SHEETS_DB_ID = os.getenv("GOOGLE_SHEETS_DB_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

# Ваши ссылки (жёстко фиксируем, чтобы бот не «рекламировал» чужих)
LINK_SITE = "https://cozy.asia"
LINK_CHANNEL_ALL = "https://t.me/SamuiRental"
LINK_CHANNEL_VILLAS = "https://t.me/arenda_vill_samui"
LINK_INSTAGRAM = "https://www.instagram.com/cozy.asia?igsh=cmt1MHA0ZmM3OTRu"

# === ПРОМО КЛАВИАТУРА ===
PROMO_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🌐 Открыть сайт", url=LINK_SITE)],
    [InlineKeyboardButton("📣 Телеграм-канал (все лоты)", url=LINK_CHANNEL_ALL)],
    [InlineKeyboardButton("🏡 Канал по виллам", url=LINK_CHANNEL_VILLAS)],
    [InlineKeyboardButton("📷 Instagram", url=LINK_INSTAGRAM)],
])

# === СОСТОЯНИЯ АНКЕТЫ ===
(
    Q_TYPE,      # 1/7 тип жилья
    Q_BUDGET,    # 2/7 бюджет
    Q_AREA,      # 3/7 район
    Q_BEDR,      # 4/7 спальни
    Q_CHECKIN,   # 5/7 дата заезда
    Q_CHECKOUT,  # 6/7 дата выезда
    Q_NOTES      # 7/7 условия/примечания
) = range(7)

# Память состояний
user_state: Dict[int, Dict[str, Any]] = {}
cta_cache: Dict[int, datetime] = {}  # чтобы не спамить промо слишком часто

# === GOOGLE SHEETS (опционально) ===
_gs_client = None
_gs_sheet = None

def init_gs():
    """Ленивая инициализация Google Sheets (если заданы ENV)."""
    global _gs_client, _gs_sheet
    if not GOOGLE_SHEETS_DB_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return
    if _gs_client and _gs_sheet:
        return
    import gspread
    from google.oauth2.service_account import Credentials

    try:
        # JSON может быть как «сырой», так и base64
        raw = GOOGLE_SERVICE_ACCOUNT_JSON
        try:
            raw = json.loads(raw)
        except Exception:
            raw = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_JSON", "{}"))  # запасной вариант
        creds = Credentials.from_service_account_info(
            raw,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        _gs_client = gspread.authorize(creds)
        _gs_sheet = _gs_client.open_by_key(GOOGLE_SHEETS_DB_ID).sheet1
        log.info("Google Sheets connected.")
    except Exception as e:
        log.warning(f"Google Sheets init failed: {e}")

def gs_append_row(row: list):
    try:
        init_gs()
        if _gs_sheet:
            _gs_sheet.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.warning(f"Google Sheets append failed: {e}")

# === ДАТЫ (любой формат) ===
def parse_any_date(s: str) -> Optional[str]:
    """Вернёт ISO yyyy-mm-dd из почти любого формата RU/EN, иначе None."""
    if not s:
        return None
    from dateutil import parser
    try:
        dt = parser.parse(s, dayfirst=True, fuzzy=True, default=datetime.utcnow())
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None

# === ФЛАГ НЕДВИЖИМОСТИ (не блокирует ответ!) ===
RE_INTENT_KEYS = (
    "недвижим", "аренд", "снять", "съём", "дом", "вилл", "апарт", "квартир",
    "пересел", "засел", "договор", "продаж", "купить", "лот", "жильё",
    "house", "villa", "apartment", "rent", "lease", "real estate"
)

def is_real_estate_intent(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in RE_INTENT_KEYS)

# === PROMO CTA (не чаще 1 раза в 3 часа) ===
def should_show_cta(user_id: int) -> bool:
    now = datetime.utcnow()
    last = cta_cache.get(user_id)
    if not last or (now - last) > timedelta(hours=3):
        cta_cache[user_id] = now
        return True
    return False

# === OPENAI ===
_openai_client = None
def oai():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client

SYSTEM_PROMPT = (
    "Ты — дружелюбный русскоязычный ассистент Cozy Asia. Отвечай коротко и по делу. "
    "Можно свободно беседовать о Самуи: климат, сезоны, районы, быт, куда сходить, "
    "логистика, визы, связь, питомцы и т.п. Если пользователь явно спрашивает о недвижимости, "
    "можно кратко сориентировать, но никаких ссылок кроме ресурсов Cozy Asia не давай."
)

async def gpt_reply(text: str, user_id: int, username: str) -> Optional[str]:
    if not OPENAI_API_KEY:
        return None
    try:
        client = oai()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",  # лёгкий и быстрый, можно заменить на GPT-5 Thinking, если доступно по ключу
            temperature=0.6,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text}
            ]
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning(f"OpenAI error: {e}")
        return None

# === СЛУЖЕБНЫЕ ===
def lead_link_to_sheet() -> str:
    if not GOOGLE_SHEETS_DB_ID:
        return ""
    return f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEETS_DB_ID}"

def fmt_user(u) -> str:
    uname = f"@{u.username}" if getattr(u, "username", None) else (u.full_name or str(u.id))
    return f"{uname} (ID: {u.id})"

def promo_text() -> str:
    return (
        "🛠️ Самый действенный способ — пройти короткую анкету командой /rent.\n"
        "Я сделаю подборку лотов (дома/апартаменты/виллы) по вашим критериям, отправлю вам, "
        "а менеджер получит заявку и свяжется для уточнений.\n\n"
        f"• Сайт: {LINK_SITE}\n"
        f"• Канал с лотами: {LINK_CHANNEL_ALL}\n"
        f"• Канал по виллам: {LINK_CHANNEL_VILLAS}\n"
        f"• Instagram: {LINK_INSTAGRAM}"
    )

# === /start ===
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Я уже тут!\n"
        "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n\n"
        "👉 Или нажмите команду /rent — задам несколько вопросов о жилье, "
        "сформирую заявку, предложу варианты и передам менеджеру. "
        "Он свяжется с вами для уточнения.",
        reply_markup=PROMO_KB
    )
    # Сбросим незавершённую анкету
    user_state.pop(update.effective_user.id, None)

# === /cancel ===
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state.pop(update.effective_user.id, None)
    await update.message.reply_text("Окей, если передумаете — пишите /rent.")

# === /diag ===
async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = [
        f"WEBHOOK_BASE: {WEBHOOK_BASE}",
        f"PORT: {PORT}",
        f"OPENAI: {'ON' if OPENAI_API_KEY else 'OFF'}",
        f"GROUP_CHAT_ID: {GROUP_CHAT_ID}",
        f"SHEETS: {'ON' if GOOGLE_SHEETS_DB_ID else 'OFF'}",
    ]
    await update.message.reply_text("\n".join(txt))

# === АНКЕТА ===
async def cmd_rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"stage": Q_TYPE, "lead_sent": False, "data": {}}
    await update.message.reply_text("Начнём подбор.\n1/7. Какой тип жилья интересует: квартира, дом или вилла?")
    return Q_TYPE

async def q_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid]["data"]["type"] = update.message.text.strip()
    await update.message.reply_text("2/7. Какой у вас бюджет в батах (месяц)?")
    return Q_BUDGET

async def q_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid]["data"]["budget"] = update.message.text.strip()
    await update.message.reply_text("3/7. В каком районе Самуи предпочтительно жить?")
    return Q_AREA

async def q_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid]["data"]["area"] = update.message.text.strip()
    await update.message.reply_text("4/7. Сколько нужно спален?")
    return Q_BEDR

async def q_bedr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid]["data"]["bedrooms"] = update.message.text.strip()
    await update.message.reply_text("5/7. Дата **заезда**? Можете в любом понятном формате (например: 01.12.2025 или 1 дек 25).")
    return Q_CHECKIN

async def q_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    date_str = parse_any_date(update.message.text)
    if not date_str:
        await update.message.reply_text("Не понял дату. Напишите ещё раз (любой формат, напр. 2025-12-01).")
        return Q_CHECKIN
    user_state[uid]["data"]["checkin"] = date_str
    await update.message.reply_text("6/7. Дата **выезда**? (любой формат)")
    return Q_CHECKOUT

async def q_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    date_str = parse_any_date(update.message.text)
    if not date_str:
        await update.message.reply_text("Не понял дату. Напишите ещё раз (любой формат, напр. 2026-01-01).")
        return Q_CHECKOUT
    user_state[uid]["data"]["checkout"] = date_str
    await update.message.reply_text("7/7. Важные условия/примечания? (близость к пляжу, с питомцами, парковка и т.п.)")
    return Q_NOTES

async def q_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = user_state.get(uid, {})
    st["data"]["notes"] = update.message.text.strip()
    st["stage"] = None
    # отправляем заявку один раз
    if not st.get("lead_sent"):
        st["lead_sent"] = True
        await finalize_lead(update, context, st["data"])
    else:
        log.info("Lead already sent; skipping duplicate.")
    # Возвращаемся к свободному общению
    user_state[uid] = {"stage": None, "lead_sent": True, "data": st["data"]}
    await update.message.reply_text(
        "Готово! Заявка сформирована и передана менеджеру. "
        "Скоро свяжемся. А пока можете задавать любые вопросы 🙂",
        reply_markup=PROMO_KB
    )
    return ConversationHandler.END

async def finalize_lead(update: Update, context: ContextTypes.DEFAULT_TYPE, data: Dict[str, str]):
    u = update.effective_user
    created = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lead_text = (
        "🆕 Новая заявка Cozy Asia\n\n"
        f"Клиент: {fmt_user(u)}\n"
        f"Тип: {data.get('type','')}\n"
        f"Район: {data.get('area','')}\n"
        f"Бюджет: {data.get('budget','')}\n"
        f"Спален: {data.get('bedrooms','')}\n"
        f"Заезд: {data.get('checkin','')}\n"
        f"Выезд: {data.get('checkout','')}\n"
        f"Условия/прим.: {data.get('notes','')}\n"
        f"Создано: {created}\n"
    )
    # В рабочую группу и менеджеру
    try:
        if GROUP_CHAT_ID:
            await context.bot.send_message(GROUP_CHAT_ID, lead_text)
    except Exception as e:
        log.warning(f"Send to group failed: {e}")
    try:
        if MANAGER_CHAT_ID:
            await context.bot.send_message(MANAGER_CHAT_ID, lead_text)
    except Exception as e:
        log.warning(f"Send to manager failed: {e}")

    # В таблицу (если настроена)
    if GOOGLE_SHEETS_DB_ID:
        row = [
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            str(u.id), getattr(u, "username", ""), getattr(u, "full_name", ""),
            data.get("type",""), data.get("budget",""), data.get("area",""),
            data.get("bedrooms",""), data.get("checkin",""), data.get("checkout",""),
            data.get("notes","")
        ]
        gs_append_row(row)

    # Пользователю — ссылка на таблицу (если есть)
    sheet_url = lead_link_to_sheet()
    if sheet_url:
        await update.message.reply_text(f"🔗 Ваша заявка зафиксирована в CRM: {sheet_url}")

# === СВОБОДНОЕ ОБЩЕНИЕ (дефолт) ===
async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text or ""

    # Если пользователь в анкете — переадресуем в ConversationHandler
    st = user_state.get(uid)
    if st and st.get("stage") is not None:
        await update.message.reply_text("Мы сейчас заполняем анкету. Напишите /cancel чтобы выйти, или отвечайте на вопрос 🙂")
        return

    # GPT в приоритете
    reply = await gpt_reply(text, uid, getattr(update.effective_user, "username", ""))

    # Мягкий фолбэк, если OpenAI отваливается
    if not reply:
        reply = (
            "Самуи: тропики; янв–март обычно суше и спокойнее, апрель — жаркий штиль, "
            "окт–дек больше дождей и волна на востоке. Можете спросить про районы, погоду и быт.\n"
        )

    # Добавим CTA, если речь про недвижимость (и не спамим чаще 1/3ч)
    if is_real_estate_intent(text) and should_show_cta(uid):
        reply += "\n\n" + promo_text()

        await update.message.reply_text(reply, reply_markup=PROMO_KB)
    else:
        await update.message.reply_text(reply)

# === СБОРКА ПРИЛОЖЕНИЯ ===
def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("ENV TELEGRAM_BOT_TOKEN is required")

    app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("diag", cmd_diag))

    # Анкета
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", cmd_rent)],
        states={
            Q_TYPE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, q_type)],
            Q_BUDGET:  [MessageHandler(filters.TEXT & ~filters.COMMAND, q_budget)],
            Q_AREA:    [MessageHandler(filters.TEXT & ~filters.COMMAND, q_area)],
            Q_BEDR:    [MessageHandler(filters.TEXT & ~filters.COMMAND, q_bedr)],
            Q_CHECKIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_checkin)],
            Q_CHECKOUT:[MessageHandler(filters.TEXT & ~filters.COMMAND, q_checkout)],
            Q_NOTES:   [MessageHandler(filters.TEXT & ~filters.COMMAND, q_notes)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Свободный чат — в самом конце
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))

    return app

# === MAIN (WEBHOOK для Render) ===
def main():
    app = build_application()

    # Чистим старый вебхук, ставим новый и запускаем сервер
    async def runner():
        await app.bot.delete_webhook(drop_pending_updates=False)
        url = f"{WEBHOOK_BASE}/webhook/{BOT_TOKEN}"
        log.info(f"Starting webhook on 0.0.0.0:{PORT} | url={url}")
        await app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=f"webhook/{BOT_TOKEN}",
            webhook_url=url,
        )

    asyncio.run(runner())

if __name__ == "__main__":
    main()
