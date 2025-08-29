import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

# ── ЛОГИ ────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("cozyasia-bot")

# ── ENV / КОНСТАНТЫ ───────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

MANAGER_CHAT_ID = int(os.getenv("MANAGER_CHAT_ID", "5978240436"))
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))

GOOGLE_SHEETS_DB_ID = os.getenv("GOOGLE_SHEETS_DB_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

LINK_SITE = "https://cozy.asia"
LINK_CHANNEL_ALL = "https://t.me/SamuiRental"
LINK_CHANNEL_VILLAS = "https://t.me/arenda_vill_samui"
LINK_INSTAGRAM = "https://www.instagram.com/cozy.asia?igsh=cmt1MHA0ZmM3OTRu"

PROMO_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🌐 Открыть сайт", url=LINK_SITE)],
    [InlineKeyboardButton("📣 Телеграм-канал (все лоты)", url=LINK_CHANNEL_ALL)],
    [InlineKeyboardButton("🏡 Канал по виллам", url=LINK_CHANNEL_VILLAS)],
    [InlineKeyboardButton("📷 Instagram", url=LINK_INSTAGRAM)],
])

(
    Q_TYPE, Q_BUDGET, Q_AREA, Q_BEDR, Q_CHECKIN, Q_CHECKOUT, Q_NOTES
) = range(7)

user_state: Dict[int, Dict[str, Any]] = {}
cta_cache: Dict[int, datetime] = {}

# ── GOOGLE SHEETS (лениво) ────────────────────────────────────────────────────
_gs_client = None
_gs_sheet = None

def init_gs():
    if not GOOGLE_SHEETS_DB_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return
    global _gs_client, _gs_sheet
    if _gs_client and _gs_sheet:
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        _gs_client = gspread.authorize(creds)
        _gs_sheet = _gs_client.open_by_key(GOOGLE_SHEETS_DB_ID).sheet1
        log.info("Google Sheets connected")
    except Exception as e:
        log.warning(f"Google Sheets init failed: {e}")

def gs_append_row(row: list):
    try:
        init_gs()
        if _gs_sheet:
            _gs_sheet.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.warning(f"Append to Sheets failed: {e}")

# ── ДАТЫ (любой формат) ───────────────────────────────────────────────────────
def parse_any_date(s: str) -> Optional[str]:
    if not s:
        return None
    try:
        from dateutil import parser
        dt = parser.parse(s, dayfirst=True, fuzzy=True, default=datetime.utcnow())
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None

# ── РЕАЛ ЕСТЕЙТ МЕТКА (не блокируем чат) ─────────────────────────────────────
RE_INTENT_KEYS = (
    "недвижим","аренд","снять","съём","дом","вилл","апарт","квартир","лот",
    "договор","продаж","купить","жильё","house","villa","apartment","rent","lease"
)
def is_real_estate_intent(t: str) -> bool:
    t = (t or "").lower()
    return any(k in t for k in RE_INTENT_KEYS)

def should_show_cta(uid: int) -> bool:
    now = datetime.utcnow()
    last = cta_cache.get(uid)
    if not last or (now - last) > timedelta(hours=3):
        cta_cache[uid] = now
        return True
    return False

# ── OPENAI: надёжный вызов + ретраи + резервные модели ───────────────────────
_openai_client = None
def oai():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client

SYSTEM_PROMPT = (
    "Ты — дружелюбный русскоязычный ассистент Cozy Asia. Отвечай по делу и кратко. "
    "Можно свободно беседовать о Самуи (погода, сезоны, районы, быт, куда сходить и т.д.). "
    "Про недвижимость — отвечай кратко и всегда показывай ресурсы Cozy Asia, не упоминай других."
)

FALLBACK_MODELS = [
    # порядок важен: сначала ENV, потом альтернативы
]

async def gpt_call(text: str) -> Optional[str]:
    """Надёжный вызов: отдельный поток, ретраи, резервные модели."""
    if not OPENAI_API_KEY:
        return None
    models = [OPENAI_MODEL] + [
        m for m in ("gpt-4o", "gpt-4o-mini", "gpt-4.1-mini", "gpt-4.1", "o4-mini")
        if m != OPENAI_MODEL
    ]

    last_err = None
    for model in models:
        for attempt in range(2):  # 2 попытки на модель
            try:
                def _do():
                    resp = oai().chat.completions.create(
                        model=model,
                        temperature=0.6,
                        messages=[
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": text}
                        ],
                        timeout=40,  # защита от зависаний
                    )
                    return (resp.choices[0].message.content or "").strip()
                return await asyncio.to_thread(_do)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                log.warning(f"OpenAI fail (model={model}, try={attempt+1}): {e}")
                await asyncio.sleep(0.7)
        # переходим к следующей модели
    log.error(f"OpenAI total failure: {last_err}")
    return None

# ── ВСПОМОГАТЕЛЬНЫЕ ──────────────────────────────────────────────────────────
def lead_link_to_sheet() -> str:
    return f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEETS_DB_ID}" if GOOGLE_SHEETS_DB_ID else ""

def fmt_user(u) -> str:
    uname = f"@{u.username}" if getattr(u, "username", None) else (u.full_name or str(u.id))
    return f"{uname} (ID: {u.id})"

def promo_text() -> str:
    return (
        "🛠️ Самый действенный способ — пройти короткую анкету /rent.\n"
        "Я сделаю подборку лотов по вашим критериям и отправлю вам, "
        "а менеджер получит заявку и свяжется.\n\n"
        f"• Сайт: {LINK_SITE}\n"
        f"• Канал с лотами: {LINK_CHANNEL_ALL}\n"
        f"• Канал по виллам: {LINK_CHANNEL_VILLAS}\n"
        f"• Instagram: {LINK_INSTAGRAM}"
    )

# ── Команды ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Я здесь! Можете спросить о Самуи — подскажу.\n\n"
        "👉 Для подбора жилья нажмите /rent — отвечу на 7 вопросов, сформирую заявку и передам менеджеру.",
        reply_markup=PROMO_KB
    )
    user_state.pop(update.effective_user.id, None)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state.pop(update.effective_user.id, None)
    await update.message.reply_text("Окей, если передумаете — /rent.")

async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wh = await context.bot.get_webhook_info()
    lines = [
        f"WEBHOOK_BASE: {WEBHOOK_BASE}",
        f"PORT: {PORT}",
        f"OPENAI: {'ON' if OPENAI_API_KEY else 'OFF'}",
        f"MODEL: {OPENAI_MODEL}",
        f"GROUP_CHAT_ID: {GROUP_CHAT_ID}",
        f"SHEETS: {'ON' if GOOGLE_SHEETS_DB_ID else 'OFF'}",
        f"Webhook URL: {wh.url or '-'}",
        f"Webhook last_error: {getattr(wh, 'last_error_message', None) or '-'}",
        f"Pending updates: {wh.pending_update_count}",
    ]
    await update.message.reply_text("\n".join(lines))

# ── Анкета /rent ─────────────────────────────────────────────────────────────
async def cmd_rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"stage": Q_TYPE, "lead_sent": False, "data": {}}
    await update.message.reply_text("Начнём подбор.\n1/7. Тип жилья: квартира, дом или вилла?")
    return Q_TYPE

async def q_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid]["data"]["type"] = update.message.text.strip()
    await update.message.reply_text("2/7. Бюджет в батах (месяц)?")
    return Q_BUDGET

async def q_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid]["data"]["budget"] = update.message.text.strip()
    await update.message.reply_text("3/7. Предпочтительный район Самуи?")
    return Q_AREA

async def q_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid]["data"]["area"] = update.message.text.strip()
    await update.message.reply_text("4/7. Сколько спален нужно?")
    return Q_BEDR

async def q_bedr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid]["data"]["bedrooms"] = update.message.text.strip()
    await update.message.reply_text("5/7. Дата заезда (любой понятный формат — 01.12.2025, 1 дек 25 и т.п.)?")
    return Q_CHECKIN

async def q_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    d = parse_any_date(update.message.text)
    if not d:
        await update.message.reply_text("Не понял дату. Ещё раз (любой формат).")
        return Q_CHECKIN
    user_state[uid]["data"]["checkin"] = d
    await update.message.reply_text("6/7. Дата выезда (любой формат)?")
    return Q_CHECKOUT

async def q_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    d = parse_any_date(update.message.text)
    if not d:
        await update.message.reply_text("Не понял дату. Ещё раз (любой формат).")
        return Q_CHECKOUT
    user_state[uid]["data"]["checkout"] = d
    await update.message.reply_text("7/7. Важные условия/примечания? (пляж, питомцы, парковка и т.п.)")
    return Q_NOTES

async def q_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = user_state.get(uid, {})
    st["data"]["notes"] = update.message.text.strip()
    st["stage"] = None
    if not st.get("lead_sent"):
        st["lead_sent"] = True
        await finalize_lead(update, context, st["data"])
    user_state[uid] = {"stage": None, "lead_sent": True, "data": st["data"]}
    await update.message.reply_text(
        "Готово! Заявка сформирована и передана менеджеру. Скоро свяжемся. "
        "А пока задавайте любые вопросы 🙂",
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
    # Группа и менеджер
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

    # Sheets
    if GOOGLE_SHEETS_DB_ID:
        row = [
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            str(u.id), getattr(u, "username", ""), getattr(u, "full_name", ""),
            data.get("type",""), data.get("budget",""), data.get("area",""),
            data.get("bedrooms",""), data.get("checkin",""), data.get("checkout",""),
            data.get("notes","")
        ]
        gs_append_row(row)
        sheet_url = lead_link_to_sheet()
        if sheet_url:
            await update.message.reply_text(f"🔗 Заявка зафиксирована: {sheet_url}")

# ── Свободный чат (дефолт) ────────────────────────────────────────────────────
async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = update.message.text or ""

    st = user_state.get(uid)
    if st and st.get("stage") is not None:
        await update.message.reply_text("Сейчас заполняем анкету. Напишите /cancel, чтобы выйти, или ответьте на вопрос 🙂")
        return

    # GPT приоритетно
    reply = await gpt_call(txt)

    if not reply:
        # Мягкий фолбэк — минимум полезной информации и всё.
        reply = ("Коротко про Самуи: янв–март суше и спокойнее; апрель — жаркий штиль; "
                 "окт–дек больше дождей и волна на востоке. Можете уточнить — подскажу.")

    # Добавляем CTA только если в тексте есть явная недвижимость и не чаще 1/3ч
    if is_real_estate_intent(txt) and should_show_cta(uid):
        reply += "\n\n" + promo_text()
        await update.message.reply_text(reply, reply_markup=PROMO_KB)
    else:
        await update.message.reply_text(reply)

# ── Сборка/запуск ─────────────────────────────────────────────────────────────
def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("ENV TELEGRAM_BOT_TOKEN is required")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("diag", cmd_diag))

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

    # Свободный чат — последним
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))

    return app

def main():
    app = build_application()

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
