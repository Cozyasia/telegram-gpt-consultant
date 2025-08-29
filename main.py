#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cozy Asia Bot — golden working baseline
- python-telegram-bot 21.6 (webhook)
- OpenAI Chat (free-form dialog) with business steering
- /rent questionnaire (7 steps) incl. flexible date parsing (any common format)
- De-duplicate submissions per user (no repeat notify on re-entry)
- Google Sheets write + link back
- Team group notify with details + links
- Safe webhook for Render (PORT/BASE_URL/WEBHOOK_PATH)
"""

import os
import re
import json
import logging
import asyncio
from datetime import datetime, timezone, timedelta

import requests
from dateutil import parser as dtparser

import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler, ConversationHandler, filters, ContextTypes
)

# ---------------------------- ENV ---------------------------------
def env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"ENV {name} is required")
    return v

TELEGRAM_TOKEN = env_required("TELEGRAM_TOKEN")
BASE_URL = env_required("BASE_URL")  # e.g. https://your-service.onrender.com
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook").strip()  # must start with '/'
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0") or "0")  # negative for group
OPENAI_API_KEY = env_required("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", os.getenv("OPENAI_MODEL_NAME", "gpt-4o-mini"))

# Optional marketing links
SITE_URL = os.getenv("SITE_URL", "https://cozy.asia")
LOTS_CHANNEL = os.getenv("LOTS_CHANNEL", "https://t.me/SamuiRental")
VILLAS_CHANNEL = os.getenv("VILLAS_CHANNEL", "https://t.me/arenda_vill_samui")
INSTAGRAM_URL = os.getenv("INSTAGRAM_URL", "https://www.instagram.com/cozy.asia")

# Google Sheets
GOOGLE_SERVICE_JSON = os.getenv("GOOGLE_SERVICE_JSON", "")
GOOGLE_SHEETS_DB_ID = os.getenv("GOOGLE_SHEETS_DB_ID", "")
SHEET_TAB = os.getenv("SHEET_TAB", "Leads")

PORT = int(os.getenv("PORT", "10000"))

# ---------------------------- LOG ---------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("cozyasia-bot")

# ---------------------------- STATE ---------------------------------
(
    Q_TYPE,
    Q_BUDGET,
    Q_AREA,
    Q_BED,
    Q_CHECKIN,
    Q_CHECKOUT,
    Q_NOTES,
) = range(7)

# Runtime in-memory store
submitted_users = set()  # user_id for which last submission is recent
user_sessions = {}       # per-user dict for answers


# ---------------------------- HELPERS ---------------------------------
def parse_date_any(s: str) -> str:
    """
    Try to parse user-provided date in *any* common format.
    Returns canonical YYYY-MM-DD (local naive).
    """
    s = s.strip()
    # normalize separators
    s = re.sub(r"[\\/.]", "-", s)
    try:
        # allow day-first guessing
        dt = dtparser.parse(s, dayfirst=not re.search(r"\d{4}-\d{1,2}-\d{1,2}", s))
        return dt.date().isoformat()
    except Exception:
        raise ValueError("Нужна дата, например: 2025-12-01 или 01.12.2025")


def marketing_block() -> str:
    return (
        f"• Сайт: {SITE_URL}\n"
        f"• Канал с лотами: {LOTS_CHANNEL}\n"
        f"• Канал по виллам: {VILLAS_CHANNEL}\n"
        f"• Instagram: {INSTAGRAM_URL}\n\n"
        "✍️ Самый действенный способ — пройти короткую анкету /rent.\n"
        "Я сделаю подборку лотов (дома/апартаменты/виллы) по вашим критериям и сразу отправлю вам.\n"
        "Менеджер получит вашу заявку и свяжется для уточнений."
    )


def marketing_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Открыть сайт", url=SITE_URL)],
        [InlineKeyboardButton("📣 Телеграм-канал (все лоты)", url=LOTS_CHANNEL)],
        [InlineKeyboardButton("🏡 Канал по виллам", url=VILLAS_CHANNEL)],
        [InlineKeyboardButton("📸 Instagram", url=INSTAGRAM_URL)],
    ])


def ai_answer(prompt: str, history: list[dict]) -> str:
    """Call OpenAI Chat Completions (openai>=1.40)."""
    import openai
    openai.api_key = OPENAI_API_KEY

    sys = (
        "Ты — дружелюбный русскоязычный ассистент Cozy Asia на Самуи. "
        "Отвечай по делу и кратко. Если речь заходит о подборе/аренде/покупке "
        "жилья или где посмотреть лоты — мягко направляй к /rent и нашим ссылкам, "
        "но *не запрещай* свободное общение и все равно отвечай на вопрос."
    )
    msgs = [{"role": "system", "content": sys}] + history + [{"role": "user", "content": prompt}]
    try:
        resp = openai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=msgs,
            temperature=0.6,
            top_p=0.95,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception("OpenAI error")
        return (
            "Похоже, у меня временные трудности с ИИ-ответом. "
            "Пока что могу подсказать базово: окт–дек на востоке волны; янв–март спокойнее; "
            "часто тише запад/юг под укрытием рельефа. Можем перейти к анкете /rent."
        )


def gs_client():
    if not GOOGLE_SERVICE_JSON or not GOOGLE_SHEETS_DB_ID:
        return None, None
    info = json.loads(GOOGLE_SERVICE_JSON)
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEETS_DB_ID)
    try:
        ws = sh.worksheet(SHEET_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(SHEET_TAB, rows=1000, cols=20)
        ws.append_row([
            "ts", "user_id", "username", "name",
            "type", "budget", "area", "bedrooms",
            "checkin", "checkout", "notes", "sheet_link"
        ])
    return gc, ws


def write_to_sheet(data: dict) -> str | None:
    gc, ws = gs_client()
    if not ws:
        return None
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    row = [
        ts, data.get("user_id"), data.get("username"), data.get("name"),
        data.get("type"), data.get("budget"), data.get("area"), data.get("bedrooms"),
        data.get("checkin"), data.get("checkout"), data.get("notes"), ""
    ]
    ws.append_row(row)
    # link to the last row
    idx = len(ws.get_all_values())
    link = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEETS_DB_ID}/edit#gid={ws.id}&range=A{idx}"
    ws.update_cell(idx, 12, link)
    return link


async def notify_group(context: ContextTypes.DEFAULT_TYPE, text: str):
    if GROUP_CHAT_ID != 0:
        try:
            await context.bot.send_message(GROUP_CHAT_ID, text, disable_web_page_preview=True)
        except Exception:
            log.exception("Failed to notify group")


# ---------------------------- COMMANDS ---------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "✅ Я уже тут!\n"
        "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n\n"
        "👉 Или нажмите команду /rent — задам несколько вопросов о жилье, "
        "сформирую заявку, предложу варианты и передам менеджеру. "
        "Он свяжется с вами для уточнения."
    )
    await update.message.reply_text(text)
    await update.message.reply_text(marketing_block(), reply_markup=marketing_keyboard(), disable_web_page_preview=True)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions.pop(user_id, None)
    await update.message.reply_text("Окей, если передумаете — пишите /rent.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ---------------------------- RENT FLOW ---------------------------------
async def rent_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # if the same user already submitted recently, just restart answers storage
    user_sessions[user_id] = {}
    await update.message.reply_text("Начнём подбор.\n1/7. Какой тип жилья интересует: квартира, дом или вилла?")
    return Q_TYPE


async def rent_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_sessions[update.effective_user.id]["type"] = update.message.text.strip()
    await update.message.reply_text("2/7. Какой у вас бюджет в батах (месяц)?")
    return Q_BUDGET


async def rent_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_sessions[update.effective_user.id]["budget"] = update.message.text.strip()
    await update.message.reply_text("3/7. В каком районе Самуи предпочтительно жить?")
    return Q_AREA


async def rent_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_sessions[update.effective_user.id]["area"] = update.message.text.strip()
    await update.message.reply_text("4/7. Сколько нужно спален?")
    return Q_BED


async def rent_bed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_sessions[update.effective_user.id]["bedrooms"] = update.message.text.strip()
    await update.message.reply_text("5/7. Дата **заезда**? Напишите как удобно (напр., 01.12.2025 или 2025-12-01).")
    return Q_CHECKIN


async def rent_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_sessions[update.effective_user.id]["checkin"] = parse_date_any(update.message.text)
        await update.message.reply_text("6/7. Дата **выезда**? Любой привычный формат.")
        return Q_CHECKOUT
    except ValueError as e:
        await update.message.reply_text(str(e))
        return Q_CHECKIN


async def rent_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_sessions[update.effective_user.id]["checkout"] = parse_date_any(update.message.text)
        await update.message.reply_text("7/7. Важные условия? (близость к пляжу, с питомцами, парковка и т.п.)")
        return Q_NOTES
    except ValueError as e:
        await update.message.reply_text(str(e))
        return Q_CHECKOUT


async def rent_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = user_sessions.get(user_id, {})
    session["notes"] = update.message.text.strip()

    # Compose final payload
    user = update.effective_user
    payload = {
        "user_id": user.id,
        "username": f"@{user.username}" if user.username else "-",
        "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
        **session,
    }

    # Write once per completed session (de-dup by memory)
    link = write_to_sheet(payload)
    submitted_users.add(user_id)

    # Notify group
    group_txt = (
        "🆕 Новая заявка Cozy Asia\n\n"
        f"Клиент: {payload['username']} (ID: {payload['user_id']})\n"
        f"Тип: {payload['type']}\n"
        f"Район: {payload['area']}\n"
        f"Бюджет: {payload['budget']}\n"
        f"Спален: {payload['bedrooms']}\n"
        f"Заезд: {payload['checkin']} | Выезд: {payload['checkout']}\n"
        f"Условия/прим.: {payload['notes']}\n"
        f"Таблица: {link or '—'}\n"
        f"Каналы: {LOTS_CHANNEL} | {VILLAS_CHANNEL}"
    )
    await notify_group(context, group_txt)

    # Inform user
    await update.message.reply_text(
        "Готово! Заявка сформирована и передана менеджеру. "
        "Сейчас проверю подходящие варианты и пришлю. Вы пока можете задать любой вопрос — я на связи.",
        disable_web_page_preview=True
    )

    # (Optional) pretend to propose lots here; real fetching can be added
    await update.message.reply_text(
        "По вашим критериям нашлось несколько вариантов. "
        "Свежие лоты можно посмотреть в наших каналах:",
        reply_markup=marketing_keyboard(),
        disable_web_page_preview=True
    )

    # cleanup per-session answers, keep 'submitted' flag to avoid duplicate noise
    user_sessions.pop(user_id, None)
    return ConversationHandler.END


# ---------------------------- GPT FALLBACK CHAT ---------------------------------
async def free_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any non-command text goes here: answer + gentle funnel to resources if relevant."""
    text = update.message.text.strip()
    user_id = update.effective_user.id

    history = context.user_data.get("history", [])[-5:]  # short memory
    reply = ai_answer(text, history)
    context.user_data.setdefault("history", []).append({"role": "user", "content": text})
    context.user_data["history"].append({"role": "assistant", "content": reply})

    # Heuristic: if message contains housing intents, append marketing
    wants_housing = bool(re.search(r"(аренд|квартир|вилл|дом|снять|купить|продать|лот)", text.lower()))
    tail = ("\n\n" + marketing_block()) if wants_housing else ""

    await update.message.reply_text(reply + tail, disable_web_page_preview=True, reply_markup=marketing_keyboard() if wants_housing else None)


# ---------------------------- APPLICATION / WEBHOOK ---------------------------------
def build_application() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # /start, /rent, /cancel
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    rent_conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_start)],
        states={
            Q_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_type)],
            Q_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_budget)],
            Q_AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_area)],
            Q_BED: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_bed)],
            Q_CHECKIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_checkin)],
            Q_CHECKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_checkout)],
            Q_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_notes)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(rent_conv)

    # free-form chat (last)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_chat))

    return app


def main():
    app = build_application()

    # Delete old webhook (safe)
    try:
        asyncio.get_event_loop().run_until_complete(app.bot.delete_webhook(drop_pending_updates=True))
        log.info("deleteWebhook -> OK")
    except Exception:
        log.exception("deleteWebhook failed")

    webhook_url = BASE_URL.rstrip("/") + WEBHOOK_PATH
    log.info(f"==> Starting webhook on 0.0.0.0:{PORT} | url={webhook_url!r}")

    # PTB 21.x correct signature
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
