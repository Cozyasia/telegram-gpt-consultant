# main.py
import os, json, logging, re
from datetime import datetime
from typing import Dict, Any, List, Optional

import dateparser
import gspread
from oauth2client.service_account import ServiceAccountCredentials

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, filters
)

# -------------------- ЛОГИ --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("cozyasia-bot")

# -------------------- УТИЛИТЫ --------------------
def env(key: str, default: Optional[str] = None) -> str:
    val = os.environ.get(key)
    if val is None:
        if default is not None:
            return default
        raise RuntimeError(f"ENV {key} is required")
    return val

def parse_int(s: str) -> Optional[int]:
    if s is None:
        return None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None

def parse_date_human(s: str) -> Optional[str]:
    if not s:
        return None
    dt = dateparser.parse(s)
    return dt.strftime("%Y-%m-%d") if dt else None

def now_utc_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

# -------------------- НАСТРОЙКИ/ССЫЛКИ --------------------
SITE_URL        = os.environ.get("SITE_URL", "https://cozy.asia")
CHANNEL_PUBLIC  = os.environ.get("CHANNEL_PUBLIC", "https://t.me/cozy_asia")
CHANNEL_SALES   = os.environ.get("CHANNEL_SALES", "https://t.me/cozyasia_sales")
MANAGER_LINK    = os.environ.get("MANAGER_LINK", "https://t.me/cozy_asia")

def resources_block() -> str:
    # красивый блок ссылок всегда ведём к /rent
    lines = [
        "📌 **Полезные ссылки Cozy Asia**",
        f"🌐 Сайт: {SITE_URL}",
        f"📣 Канал: {CHANNEL_PUBLIC}",
        f"🏡 Подбор/продажи: {CHANNEL_SALES}",
        f"👤 Написать менеджеру: {MANAGER_LINK}",
        "",
        "👉 Готовы к подбору? Нажмите /rent — оформим заявку и свяжем с менеджером."
    ]
    return "\n".join(lines)

# -------------------- GOOGLE SHEETS --------------------
_gs_client = None
_sheet = None

def init_sheets() -> None:
    global _gs_client, _sheet
    if _sheet:
        return

    creds_json = env("GOOGLE_CREDS_JSON")
    try:
        # допускаем многострочный JSON из переменной окружения
        info = json.loads(creds_json)
    except json.JSONDecodeError:
        # иногда переносы / экранирование — пробуем безопасно
        info = json.loads(creds_json.encode("utf-8").decode("unicode_escape"))

    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
    _gs_client = gspread.authorize(creds)

    sheet_id = env("GOOGLE_SHEETS_ID")
    sh = _gs_client.open_by_key(sheet_id)
    _sheet = sh.worksheet("Leads")  # имя вкладки как в твоей таблице

def write_lead_row(row: List[Any]) -> None:
    init_sheets()
    _sheet.append_row(row, value_input_option="USER_ENTERED")

# -------------------- СОСТОЯНИЕ ОПРОСА --------------------
# Храним состояние опроса прямо в user_data
QUESTIONS = [
    ("type",     "1/7: какой тип жилья интересует? (квартира/дом/вилла)"),
    ("people",   "2/7: на сколько человек? (числом)"),
    ("budget",   "3/7: бюджет (в бат/ночь или мес., любым форматом)"),
    ("location", "4/7: какой район Самуи предпочитаете?"),
    ("checkin",  "5/7: дата заезда (любой формат: 2025-12-01, 01.12.2025 и т. п.)"),
    ("checkout", "6/7: дата выезда (любой формат)"),
    ("notes",    "7/7: важные условия/примечания (питомцы, бассейн, парковка и т.п.)"),
]

START_GREETING = (
    "✅ Я уже тут!\n"
    "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n"
    "👉 Или нажмите команду /rent — я задам несколько вопросов о жилье, "
    "сформирую заявку, предложу варианты и передам менеджеру. Он свяжется с вами для уточнения деталей и бронирования."
)

def reset_flow(data: Dict[str, Any]) -> None:
    data["flow"] = None
    data["answers"] = {}
    data["q_index"] = 0

# -------------------- ХЕНДЛЕРЫ --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_flow(context.user_data)
    await update.message.reply_text(START_GREETING)

async def cmd_rent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_flow(context.user_data)
    context.user_data["flow"] = "rent"
    await update.message.reply_text("Запускаю короткую анкету. Вопрос 1.")
    await update.message.reply_text(QUESTIONS[0][1])

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()

    # если в режиме опроса
    if context.user_data.get("flow") == "rent":
        q_index = context.user_data.get("q_index", 0)
        key, _ = QUESTIONS[q_index]
        context.user_data.setdefault("answers", {})[key] = text

        q_index += 1
        context.user_data["q_index"] = q_index

        if q_index < len(QUESTIONS):
            await update.message.reply_text(QUESTIONS[q_index][1])
            return

        # анкета завершена -> формируем
        ans = context.user_data["answers"]
        chat = update.effective_chat
        username = (update.effective_user.username or "—")
        chat_id = chat.id

        # нормализуем цифры/даты
        people  = parse_int(ans.get("people", ""))
        budget  = parse_int(ans.get("budget", ""))
        checkin = parse_date_human(ans.get("checkin", ""))
        checkout= parse_date_human(ans.get("checkout", ""))

        # запись в таблицу
        try:
            write_lead_row([
                now_utc_str(),
                str(chat_id),
                username,
                ans.get("location", ""),
                ans.get("type", ""),
                budget if budget is not None else ans.get("budget", ""),
                people if people is not None else ans.get("people", ""),
                ans.get("notes", ""),
                checkin or ans.get("checkin", ""),
                checkout or ans.get("checkout", ""),
            ])
        except Exception as e:
            log.exception("Sheets append failed")
            await update.message.reply_text("⚠️ Не удалось записать в таблицу (мы это уже правим). Заявка все равно принята.")

        # уведомление в рабочую группу
        try:
            group_id = int(env("GROUP_CHAT_ID"))
            await context.bot.send_message(
                chat_id=group_id,
                text=(
                    "🆕 **Новая заявка Cozy Asia**\n"
                    f"Клиент: @{username} (ID: {chat_id})\n"
                    f"Тип: {ans.get('type','')}\n"
                    f"Район: {ans.get('location','')}\n"
                    f"Бюджет: {budget if budget is not None else ans.get('budget','')}\n"
                    f"Спален: {people if people is not None else ans.get('people','')}\n"
                    f"Check-in: {checkin or ans.get('checkin','')}\n"
                    f"Check-out: {checkout or ans.get('checkout','')}\n"
                    f"Условия/прим.: {ans.get('notes','')}\n"
                    f"Создано: {now_utc_str()} UTC"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            log.exception("Group notify failed")

        # подтверждение клиенту
        await update.message.reply_text(
            "📝 Заявка сформирована и передана менеджеру.\n\n"
            f"Тип: {ans.get('type','')}\n"
            f"Район: {ans.get('location','')}\n"
            f"Спален: {people if people is not None else ans.get('people','')}\n"
            f"Бюджет: {budget if budget is not None else ans.get('budget','')}\n"
            f"Check-in: {checkin or ans.get('checkin','')}\n"
            f"Check-out: {checkout or ans.get('checkout','')}\n"
            f"Условия: {ans.get('notes','')}\n\n"
            "Сейчас подберу и пришлю подходящие варианты, а менеджер уже в курсе и свяжется при необходимости. "
            "Можно продолжать свободное общение — спрашивайте про районы, сезонность и т.д."
        )

        reset_flow(context.user_data)
        return

    # свободный диалог → всегда ненавязчиво ведём к /rent и даём блок ресурсов
    # (тут можно вставлять свою генерацию ответов/руководства)
    reply = (
        "Понимаю. Могу подсказать по острову, районам и сезонности. "
        "Если цель — подобрать жильё под ваши параметры, быстрее всего пройти короткий опрос — команда /rent.\n\n"
        + resources_block()
    )
    await update.message.reply_text(reply, parse_mode="Markdown")

# -------------------- ИНИЦИАЛИЗАЦИЯ --------------------
def build_application() -> Application:
    token = env("TELEGRAM_TOKEN")
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rent", cmd_rent))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app

def run_webhook(app: Application) -> None:
    base = env("WEBHOOK_BASE").rstrip("/")
    path = env("WEBHOOK_PATH", "/webhook")
    port = int(env("PORT", "10000"))
    public_url = f"{base}{path}"

    log.info("=> run_webhook port=%s url=%s", port, public_url)

    # ВАЖНО: для твоей версии PTB нужен параметр webhook_url
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        webhook_path=path,
        webhook_url=public_url,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    application = build_application()
    run_webhook(application)
