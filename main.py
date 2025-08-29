import os
import re
import time
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ---------- ЛОГИ ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cozyasia-bot")


# ---------- КОНСТАНТЫ/ENV ----------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

# Webhook базовый URL сервиса Render (Primary URL), БЕЗ завершающего "/"
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").strip()
if not WEBHOOK_BASE:
    # запасной путь: Render иногда прокидывает внешку сюда
    WEBHOOK_BASE = os.getenv("RENDER_EXTERNAL_URL", "").strip()

# Нормализация
if WEBHOOK_BASE.endswith("/"):
    WEBHOOK_BASE = WEBHOOK_BASE[:-1]
if WEBHOOK_BASE and not WEBHOOK_BASE.startswith("http"):
    WEBHOOK_BASE = "https://" + WEBHOOK_BASE

WEBHOOK_PATH = "/webhook"  # без токена в пути — так надёжнее
PORT = int(os.getenv("PORT", "10000"))

# Куда дублировать заявки
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID", "").strip()  # пример: "5978240436"
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "").strip()      # пример: "-1002222333444"

# Google Sheets (необязательно)
GOOGLE_SHEETS_DB_ID = os.getenv("GOOGLE_SHEETS_DB_ID", "").strip()  # ID таблицы
GOOGLE_SERVICE_JSON = os.getenv("GOOGLE_SERVICE_JSON", "").strip()  # JSON сервис-аккаунта (как строка)
GOOGLE_SHEET_TAB = os.getenv("GOOGLE_SHEET_TAB", "Leads").strip()   # имя листа

# ---------- ССЫЛКИ COZY ASIA ----------
SITE_URL = "https://cozy.asia"
TG_ALL_LOTS = "https://t.me/SamuiRental"
TG_VILLAS = "https://t.me/arenda_vill_samui"
INSTA_URL = "https://www.instagram.com/cozy.asia?igsh=cmt1MHA0ZmM3OTRu"

# ---------- ГЛОБАЛЬНАЯ ПАМЯТЬ ----------
# Запоминаем состояние пользователей (на уровне процесса)
USER_STATE: Dict[int, Dict[str, Any]] = {}

# Conversation states
(
    Q_TYPE,      # тип жилья
    Q_BUDGET,    # бюджет/мес
    Q_AREA,      # район
    Q_BEDROOMS,  # спальни
    Q_CHECKIN,   # дата заезда
    Q_CHECKOUT,  # дата выезда
    Q_NOTES,     # условия
) = range(7)

# ---------- УТИЛЫ ----------

def is_real_estate_intent(text: str) -> bool:
    """Грубая эвристика: если речь про недвижимость — вернём True."""
    if not text:
        return False
    t = text.lower()
    kw = [
        "аренд", "снять", "сдаё", "сдаю", "дом", "вилла", "кварти", "недвиж",
        "купить", "покупк", "продать", "продаж", "лот", "жильё", "жилье",
        "apart", "villa", "house", "rent", "rental", "lease", "real estate",
    ]
    return any(k in t for k in kw)


def promo_block() -> str:
    return (
        "📌 По вопросам недвижимости лучше сразу у нас:\n"
        f"• Сайт: {SITE_URL}\n"
        f"• Канал с лотами: {TG_ALL_LOTS}\n"
        f"• Канал по виллам: {TG_VILLAS}\n"
        f"• Instagram: {INSTA_URL}\n\n"
        "✍️ Самый действенный способ — пройти короткую анкету командой /rent.\n"
        "Я сделаю подборку лотов по вашим критериям и передам менеджеру.\n"
        "Связаться с менеджером напрямую можно **после заполнения заявки**."
    )


def promo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Открыть сайт", url=SITE_URL)],
        [InlineKeyboardButton("📣 Телеграм-канал (все лоты)", url=TG_ALL_LOTS)],
        [InlineKeyboardButton("🏡 Канал по виллам", url=TG_VILLAS)],
        [InlineKeyboardButton("📷 Instagram", url=INSTA_URL)],
    ])


def parse_date_any(s: str) -> Optional[str]:
    """Парсим дату в кучу распространённых форматов. Возвращаем YYYY-MM-DD или None."""
    if not s:
        return None
    s = s.strip().replace(" ", "")

    # Пробуем популярные форматы явно
    fmts = [
        "%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%Y.%m.%d",
        "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d",
        "%d.%m.%y",  "%d-%m-%y",  "%y.%m.%d", "%y-%m-%d",
        "%d%m%Y",    "%Y%m%d",
    ]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Попытка с dateutil, если установлен
    try:
        from dateutil import parser as dateparser  # type: ignore
        dt = dateparser.parse(s, dayfirst=True, yearfirst=False, fuzzy=True)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def set_user_state(user_id: int, **kwargs):
    st = USER_STATE.get(user_id, {})
    st.update(kwargs)
    USER_STATE[user_id] = st


def get_user_state(user_id: int) -> Dict[str, Any]:
    return USER_STATE.get(user_id, {})


# ---------- OPENAI ----------
def ai_answer_sync(user_text: str, user_name: str = "") -> str:
    """
    Синхронный вызов OpenAI. Возвращает текст ответа.
    """
    if not OPENAI_API_KEY:
        # Без ключа отвечаем дефолтом
        return (
            "Я на связи. Готов помочь с любыми вопросами!\n\n" + promo_block()
        )

    try:
        # openai>=1.40.0
        from openai import OpenAI  # type: ignore
        client = OpenAI(api_key=OPENAI_API_KEY)

        system_prompt = (
            "Ты дружелюбный помощник Cozy Asia на Самуи. "
            "Отвечай на любые вопросы: погода, районы, быт, досуг и т.д. "
            "Если пользователь интересуется недвижимостью (аренда/покупка/продажа, дома, виллы, квартиры, лоты), "
            "в дополнение к ответу аккуратно предложи посмотреть варианты у нас, "
            "покажи ссылки (сайт, канал с лотами, канал по виллам, Instagram) и предложи команду /rent. "
            "НИКОГДА не упоминай конкурирующие агентства, сайты или каналы. "
            "Контакт менеджера даём только ПОСЛЕ заполнения анкеты."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.5,
        )
        txt = (resp.choices[0].message.content or "").strip()
        # Мягкое добавление промо, если вопрос про недвижимость
        if is_real_estate_intent(user_text):
            txt = f"{txt}\n\n{promo_block()}"
        return txt
    except Exception as e:
        log.exception("OpenAI error: %s", e)
        # Фоллбэк
        return (
            "Похоже, ИИ сейчас занят, но я на связи и могу помочь.\n\n" + promo_block()
        )


# ---------- АНКЕТА (ConversationHandler) ----------

async def rent_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    set_user_state(user.id, in_form=True, form={}, submitted=False)
    await update.message.reply_text(
        "Начнём подбор.\n"
        "1/7. Какой тип жилья интересует: квартира, дом или вилла?",
        reply_markup=ReplyKeyboardRemove(),
    )
    return Q_TYPE


async def rent_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_state(update.effective_user.id, form={"type": update.message.text})
    await update.message.reply_text("2/7. Какой бюджет в батах (месяц)?")
    return Q_BUDGET


async def rent_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(update.effective_user.id)
    st["form"]["budget"] = update.message.text
    await update.message.reply_text("3/7. В каком районе Самуи предпочитаете жить?")
    return Q_AREA


async def rent_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(update.effective_user.id)
    st["form"]["area"] = update.message.text
    await update.message.reply_text("4/7. Сколько нужно спален?")
    return Q_BEDROOMS


async def rent_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(update.effective_user.id)
    st["form"]["bedrooms"] = update.message.text
    await update.message.reply_text("5/7. Дата заезда? Можешь писать в любом формате (например, 01.12.2025 или 2025-12-01).")
    return Q_CHECKIN


async def rent_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(update.effective_user.id)
    dt = parse_date_any(update.message.text)
    if not dt:
        await update.message.reply_text("Не понял дату. Напиши ещё раз (любой привычный формат).")
        return Q_CHECKIN
    st["form"]["checkin"] = dt
    await update.message.reply_text("6/7. Дата выезда? Тоже в любом формате.")
    return Q_CHECKOUT


async def rent_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(update.effective_user.id)
    dt = parse_date_any(update.message.text)
    if not dt:
        await update.message.reply_text("Не понял дату. Напиши ещё раз (любой привычный формат).")
        return Q_CHECKOUT
    st["form"]["checkout"] = dt
    await update.message.reply_text("7/7. Важные условия? (близость к пляжу, с питомцами, парковка и т.п.)")
    return Q_NOTES


def _format_lead_card(user: Any, form: Dict[str, Any]) -> str:
    uname = f"@{user.username}" if user and user.username else f"{user.full_name}"
    uid = user.id if user else "—"
    card = (
        "🆕 Новая заявка Cozy Asia\n\n"
        f"Клиент: {uname} (ID: {uid})\n"
        f"Тип: {form.get('type','—')}\n"
        f"Район: {form.get('area','—')}\n"
        f"Бюджет: {form.get('budget','—')}\n"
        f"Спален: {form.get('bedrooms','—')}\n"
        f"Заезд: {form.get('checkin','—')}\n"
        f"Выезд: {form.get('checkout','—')}\n"
        f"Условия/прим.: {form.get('notes','—')}\n"
        f"Создано: {now_iso()}"
    )
    return card


async def _push_to_sheets(form: Dict[str, Any], user: Any) -> Optional[str]:
    """Пишем в Google Sheets, если настроены ключи. Возвращает URL листа/строки (если возможно) или None."""
    if not GOOGLE_SHEETS_DB_ID or not GOOGLE_SERVICE_JSON:
        return None
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore

        service_info = json.loads(GOOGLE_SERVICE_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(service_info, scopes=scopes)
        gc = gspread.authorize(creds)

        sh = gc.open_by_key(GOOGLE_SHEETS_DB_ID)
        ws = sh.worksheet(GOOGLE_SHEET_TAB)

        row = [
            now_iso(),
            user.id if user else "",
            f"@{user.username}" if user and user.username else (user.full_name if user else ""),
            form.get("type",""),
            form.get("budget",""),
            form.get("area",""),
            form.get("bedrooms",""),
            form.get("checkin",""),
            form.get("checkout",""),
            form.get("notes",""),
        ]
        ws.append_row(row)
        try:
            return f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEETS_DB_ID}"
        except Exception:
            return None
    except Exception as e:
        log.exception("Sheets error: %s", e)
        return None


async def rent_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Финал анкеты: сохраняем, уведомляем группу/менеджера, пишем в таблицу, возвращаемся к GPT-чату."""
    user = update.effective_user
    st = get_user_state(user.id)
    st["form"]["notes"] = update.message.text

    # Анти-дубликат: помечаем как отправленную
    st["submitted"] = True
    st["in_form"] = False
    st["last_submission_at"] = time.time()

    form = st["form"]
    card = _format_lead_card(user, form)

    # В таблицу
    sheet_url = await _push_to_sheets(form, user)
    if sheet_url:
        card += f"\n\n🗂 Таблица: {sheet_url}"

    # Уведомления
    if GROUP_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=card,
            )
        except Exception as e:
            log.warning("Send to group failed: %s", e)
    if MANAGER_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=MANAGER_CHAT_ID,
                text=card,
            )
        except Exception as e:
            log.warning("Send to manager failed: %s", e)

    # Пользователю — подтверждение + ссылки + кнопки
    await update.message.reply_text(
        "Заявка сформирована и передана менеджеру. Он свяжется с вами для уточнений.\n\n"
        "А пока можете посмотреть лоты и новости у нас:\n" + promo_block(),
        reply_markup=promo_keyboard(),
        disable_web_page_preview=True,
    )

    # теоретически здесь можно было бы подтянуть «автоподбор», но без доступа к каналам/БД оставим ссылки
    return ConversationHandler.END


async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(update.effective_user.id)
    st["in_form"] = False
    await update.message.reply_text("Окей, если передумаете — пишите /rent.")
    return ConversationHandler.END


# ---------- СВОБОДНЫЙ ЧАТ (GPT) ----------

async def free_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Маршрутизатор свободного общения: отвечает GPT и при необходимости добавляет промо."""
    user = update.effective_user
    st = get_user_state(user.id)

    # Если пользователь в анкете — игнорим этот хендлер
    if st.get("in_form"):
        return

    txt = update.message.text or ""

    # Анти-дубликат после только что отправленной анкеты: просто чатимся
    answer = await context.application.run_in_threadpool(ai_answer_sync, txt, user.full_name)
    # Добавим кнопки, но только если реально был "недвижимый" вопрос,
    # чтобы не мешать обычным ответам про погоду и т.п.
    if is_real_estate_intent(txt):
        await update.message.reply_text(
            answer,
            reply_markup=promo_keyboard(),
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(
            answer,
            disable_web_page_preview=True,
        )


# ---------- СЛУЖЕБНЫЕ КОМАНДЫ ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    set_user_state(user.id, in_form=False)  # на всякий случай
    hello = (
        "✅ Я уже тут!\n"
        "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n\n"
        "👉 Или нажмите команду /rent — задам несколько вопросов о жилье, "
        "сформирую заявку, предложу варианты и передам менеджеру. "
        "Он свяжется с вами для уточнения."
    )
    await update.message.reply_text(hello)
    # лёгкое напоминание, но без блокировки чата
    await update.message.reply_text(
        "Могу ответить на любые вопросы. По недвижимости — жмите /rent или смотрите ссылки ниже.\n\n"
        + promo_block(),
        reply_markup=promo_keyboard(),
        disable_web_page_preview=True,
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await rent_cancel(update, context)


async def diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    url = f"{WEBHOOK_BASE}{WEBHOOK_PATH}" if WEBHOOK_BASE else "(not set)"
    env_ok = {
        "BOT_TOKEN": bool(BOT_TOKEN),
        "OPENAI_API_KEY": bool(OPENAI_API_KEY),
        "WEBHOOK_BASE": WEBHOOK_BASE,
        "WEBHOOK_PATH": WEBHOOK_PATH,
        "GROUP_CHAT_ID": GROUP_CHAT_ID or "(none)",
        "MANAGER_CHAT_ID": MANAGER_CHAT_ID or "(none)",
        "SHEETS_ID?": bool(GOOGLE_SHEETS_DB_ID),
        "SVC_JSON?": bool(GOOGLE_SERVICE_JSON),
        "MODEL": OPENAI_MODEL,
    }
    pretty = "\n".join([f"{k}: {v}" for k, v in env_ok.items()])
    await update.message.reply_text(
        f"Webhook URL: {url}\n\n{pretty}"
    )


# ---------- СБОРКА ПРИЛОЖЕНИЯ ----------

def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("ENV TELEGRAM_BOT_TOKEN is required")

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Анкета
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_start)],
        states={
            Q_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_type)],
            Q_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_budget)],
            Q_AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_area)],
            Q_BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_bedrooms)],
            Q_CHECKIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_checkin)],
            Q_CHECKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_checkout)],
            Q_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_done)],
        },
        fallbacks=[CommandHandler("cancel", rent_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Служебные команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("diag", diag))

    # Свободное общение (последним, чтобы не перехватывать команды)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_chat))

    return app


def main():
    if not WEBHOOK_BASE:
        raise RuntimeError("ENV WEBHOOK_BASE is required (Render Primary URL)")

    app = build_application()

    url = f"{WEBHOOK_BASE}{WEBHOOK_PATH}"
    log.info("==> Starting webhook on 0.0.0.0:%s | url=%r", PORT, url)

    # run_webhook сам поставит setWebhook(webhook_url=url)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=url,
        # необязательно, но полезно:
        secret_token=None,  # если хочешь секрет — добавь ENV и передай сюда
    )


if __name__ == "__main__":
    main()
