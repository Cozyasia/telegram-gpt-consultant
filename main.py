# main.py
# Cozy Asia Bot — финальная стабильная сборка (вебхук для Render)
# Требуемые ENV:
# TELEGRAM_TOKEN, WEBHOOK_BASE (например https://<service>.onrender.com),
# WEBHOOK_PATH (например /webhook), PORT (например 10000),
# GROUP_CHAT_ID (число, ид чата для уведомлений, может быть отрицательным),
# GOOGLE_CREDS_JSON (полный JSON сервис-аккаунта в одну строку),
# GOOGLE_SHEET_ID или GOOGLE_SHEET_URL (любой один из них)

import os
import json
import asyncio
from datetime import datetime
from typing import Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials

from dateutil import parser as dateparser  # уже используется в твоём проекте
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==========================
# ТЕКСТЫ: приветствие и брендинг
# ==========================

START_TEXT = (
    "✅ Я уже тут!\n"
    "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n"
    "👉 Или нажмите команду <b>/rent</b> — я задам несколько вопросов о жилье, "
    "сформирую заявку, предложу варианты и передам менеджеру. Он свяжется с вами "
    "для уточнения деталей и бронирования."
)

BRAND_BLOCK = (
    "🔗 <b>Полезные ссылки Cozy Asia</b>\n"
    "🌐 Сайт: <a href=\"https://cozy.asia\">cozy.asia</a>\n"
    "📣 Канал о виллах и жизни на Самуи: <a href=\"https://t.me/cozy_asia\">@cozy_asia</a>\n"
    "📜 Гайды и правила: <a href=\"https://t.me/cozy_asia_rules\">@cozy_asia_rules</a>\n"
    "👤 Менеджер: <a href=\"https://t.me/cozy_asia_manager\">@cozy_asia_manager</a>\n\n"
    "✳️ Чтобы перейти к подбору — напишите <b>/rent</b>."
)

CTA_SHORT = (
    "🧭 Нужен персональный подбор жилья? Напишите <b>/rent</b> — запущу короткую анкету "
    "и передам менеджеру."
)

# ==========================
# ВСПОМОГАТЕЛЬНОЕ
# ==========================

RENT_FIELDS = [
    ("type",      "1/7: какой тип жилья интересует? <i>(квартира/дом/вилла)</i>"),
    ("area",      "2/7: район/локация на Самуи <i>(например: Ламай, Маенам, Бопут…)</i>"),
    ("bedrooms",  "3/7: сколько спален нужно?"),
    ("budget",    "4/7: бюджет в батах (за месяц)"),
    ("checkin",   "5/7: дата заезда <i>(любой формат: 2025-12-01, 01.12.2025…)</i>"),
    ("checkout",  "6/7: дата выезда <i>(любой формат)</i>"),
    ("notes",     "7/7: важные условия/примечания <i>(питомцы, бассейн, парковка…)</i>"),
]

def env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"ENV {name} is required")
    return v

def try_parse_date(text: str) -> str:
    try:
        dt = dateparser.parse(text, dayfirst=True)
        if dt:
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    return text.strip()

# ==========================
# GOOGLE SHEETS
# ==========================

_gs_client = None
_gs_worksheet = None

def gs_init_once() -> None:
    """Ленивая инициализация клиента и таблицы (один раз на процесс)."""
    global _gs_client, _gs_worksheet
    if _gs_client and _gs_worksheet:
        return

    creds_json_raw = env("GOOGLE_CREDS_JSON")
    # допускаем как «однострочный JSON», так и с \n — оба варианта у тебя встречались
    creds_info = json.loads(creds_json_raw)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(creds_info, scopes=scopes)
    _gs_client = gspread.authorize(credentials)

    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    sheet_url = os.getenv("GOOGLE_SHEET_URL")
    if sheet_id:
        sh = _gs_client.open_by_key(sheet_id)
    elif sheet_url:
        sh = _gs_client.open_by_url(sheet_url)
    else:
        raise RuntimeError("ENV GOOGLE_SHEET_ID or GOOGLE_SHEET_URL is required")

    # вкладка Leads (создастся автоматически, если её ещё нет)
    try:
        _gs_worksheet = sh.worksheet("Leads")
    except gspread.exceptions.WorksheetNotFound:
        _gs_worksheet = sh.add_worksheet(title="Leads", rows=1000, cols=20)
        _gs_worksheet.append_row([
            "created_at", "chat_id", "username",
            "type", "area", "bedrooms", "budget",
            "checkin", "checkout", "notes"
        ])

def gs_append_lead(row: list) -> None:
    gs_init_once()
    _gs_worksheet.append_row(row, value_input_option="USER_ENTERED")

# ==========================
# ХЕНДЛЕРЫ
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id=chat_id, text=START_TEXT, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )
    await context.bot.send_message(
        chat_id=chat_id, text=BRAND_BLOCK, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )

async def cmd_rent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["rent"] = {"step": 0, "data": {}}
    await update.message.reply_text(RENT_FIELDS[0][1], parse_mode=ParseMode.HTML)

def is_in_rent(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return "rent" in context.user_data and isinstance(context.user_data["rent"], dict)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()

    # В процессе анкеты
    if is_in_rent(context):
        rent = context.user_data["rent"]
        step = rent.get("step", 0)
        data = rent.get("data", {})

        key, prompt = RENT_FIELDS[step]
        value = text

        # аккуратно нормализуем даты
        if key in ("checkin", "checkout"):
            value = try_parse_date(value)

        data[key] = value
        rent["data"] = data
        step += 1

        if step < len(RENT_FIELDS):
            rent["step"] = step
            context.user_data["rent"] = rent
            await update.message.reply_text(RENT_FIELDS[step][1], parse_mode=ParseMode.HTML)
            return

        # анкета завершена -> сохраняем и уведомляем
        context.user_data.pop("rent", None)
        await finalize_rent(update, context, data)
        return

    # Свободное общение: выводим короткий CTA + бренд-блок
    await update.message.reply_text(CTA_SHORT, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    await update.message.reply_text(BRAND_BLOCK, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def finalize_rent(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict) -> None:
    # 1) Сохраняем в таблицу
    try:
        created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        chat_id = update.effective_chat.id
        username = (update.effective_user.username or "—")
        row = [
            created_at, str(chat_id), username,
            data.get("type", ""), data.get("area", ""), data.get("bedrooms", ""),
            data.get("budget", ""), data.get("checkin", ""), data.get("checkout", ""),
            data.get("notes", "")
        ]
        gs_append_lead(row)
        saved_ok = True
    except Exception as e:
        saved_ok = False

    # 2) Сообщение пользователю (резюме заявки)
    card = (
        "📝 <b>Заявка сформирована и передана менеджеру.</b>\n\n"
        f"Тип: {data.get('type','')}\n"
        f"Район: {data.get('area','')}\n"
        f"Спален: {data.get('bedrooms','')}\n"
        f"Бюджет: {data.get('budget','')}\n"
        f"Check-in: {data.get('checkin','')}\n"
        f"Check-out: {data.get('checkout','')}\n"
        f"Условия: {data.get('notes','')}\n\n"
        "Сейчас подберу и пришлю подходящие варианты, а менеджер уже в курсе и свяжется при необходимости. "
        "Можно продолжать свободное общение — спрашивайте про районы, сезонность и т.д."
    )
    await update.message.reply_text(card, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    # 3) Уведомление в рабочую группу
    try:
        group_id = int(env("GROUP_CHAT_ID"))
        uname = update.effective_user.username
        mention = f"@{uname}" if uname else "@—"
        created_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        group_msg = (
            "🆕 <b>Новая заявка Cozy Asia</b>\n"
            f"Клиент: {mention} (ID: {update.effective_user.id})\n"
            f"Тип: {data.get('type','')}\n"
            f"Район: {data.get('area','')}\n"
            f"Бюджет: <a href=\"https://t.me/{uname}\">{data.get('budget','')}</a>\n" if uname else
            f"Бюджет: {data.get('budget','')}\n"
        )
        group_msg += (
            f"Спален: {data.get('bedrooms','')}\n"
            f"Check-in: {data.get('checkin','')}\n"
            f"Check-out: {data.get('checkout','')}\n"
            f"Условия/прим.: {data.get('notes','')}\n"
            f"Создано: {created_utc}"
        )

        await context.bot.send_message(
            chat_id=group_id,
            text=group_msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    except Exception:
        pass

# ==========================
# ИНИЦИАЛИЗАЦИЯ И ЗАПУСК (WEBHOOK)
# ==========================

def build_application() -> Application:
    token = env("TELEGRAM_TOKEN")
    app: Application = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rent", cmd_rent))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app

def run_webhook(app: Application) -> None:
    base = env("WEBHOOK_BASE").rstrip("/")
    path = env("WEBHOOK_PATH", "/webhook")
    port = int(env("PORT", "10000"))
    webhook_url = f"{base}{path}"

    # run_webhook сам управляет установкой вебхука
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url=webhook_url,
        webhook_path=path,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    application = build_application()
    run_webhook(application)
