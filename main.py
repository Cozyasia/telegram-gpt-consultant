# main.py — Cozy Asia Bot (python-telegram-bot v21.6, webhook/Render)
# -----------------------------------------------------------------------------
# Возможности:
# • /start — приветствие
# • /rent — анкета (7 шагов): type → budget → area → bedrooms → check-in → check-out → notes
#   - даты распознаются в ЛЮБОМ привычном формате и сохраняются как YYYY-MM-DD
#   - после анкеты: запись в Google Sheets (если настроено), подборки-ссылки из каналов,
#     уведомления менеджеру и в рабочую группу, кнопка “Написать менеджеру”
#   - анти-дубликаты (повторная отправка в течение 15 мин. не создаёт новую запись)
# • Свободный GPT-чат (OpenAI): отвечает на любые темы
#   - если разговор уходит к недвижимости — НЕ прерывает ответ, а ДОБАВЛЯЕТ ваш CTA
#     (сайт/каналы/Instagram + /rent). Никогда не рекламирует сторонние агентства/агрегаторы
# • /id, /groupid, /diag — сервисные команды
#
# Переменные окружения (ENV):
#   TELEGRAM_BOT_TOKEN (обязательно)
#   OPENAI_API_KEY     (обязательно)
#   WEBHOOK_BASE или RENDER_EXTERNAL_URL (обязательно, публичный URL сервиса)
#   GOOGLE_SHEETS_DB_ID                (опционально — ID таблицы)
#   GOOGLE_SERVICE_ACCOUNT_JSON        (опционально — JSON сервис-аккаунта Google, одной строкой)
#   GOOGLE_SHEETS_SHEET_NAME=Leads     (опционально)
#   GROUP_CHAT_ID                      (опционально — ID рабочей группы)
#
# Запуск на Render: python main.py
# -----------------------------------------------------------------------------

from __future__ import annotations
import os, json, logging, time, urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, List

import requests
from dateutil import parser as dtparser
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

# ─────────────── ЛОГИ
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("cozyasia-bot")

# ─────────────── КОНСТАНТЫ/ССЫЛКИ (замени при необходимости)
WEBSITE_URL       = "https://cozy.asia"
TG_CHANNEL_MAIN   = "https://t.me/SamuiRental"
TG_CHANNEL_VILLAS = "https://t.me/arenda_vill_samui"
INSTAGRAM_URL     = "https://www.instagram.com/cozy.asia?igsh=cmt1MHA0ZmM3OTRu"
MAIN_CH_USERNAME   = "SamuiRental"
VILLAS_CH_USERNAME = "arenda_vill_samui"

# Менеджер (контакт показываем ТОЛЬКО ПОСЛЕ анкеты)
MANAGER_TG_URL  = "https://t.me/Cozy_asia"  # @Cozy_asia
MANAGER_CHAT_ID = 5978240436                # личка менеджера

# Рабочая группа (можно задать через ENV GROUP_CHAT_ID)
GROUP_CHAT_ID: Optional[int] = None
if os.getenv("GROUP_CHAT_ID"):
    try:
        GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))
    except Exception:
        log.warning("GROUP_CHAT_ID из ENV не int: %r", os.getenv("GROUP_CHAT_ID"))

# ─────────────── ТЕКСТЫ/КЛЮЧИ
START_TEXT = (
    "✅ Я уже тут!\n"
    "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n"
    "👉 Или нажмите команду /rent — задам несколько вопросов о жилье, "
    "сформирую заявку, предложу варианты и передам менеджеру. Он свяжется с вами для уточнения."
)

REALTY_KEYWORDS = {
    "аренда","сдать","сниму","снять","дом","вилла","квартира","комнаты","спальни",
    "покупка","купить","продажа","продать","недвижимость","кондо","condo","таунхаус",
    "bungalow","bungalo","house","villa","apartment","rent","buy","sale","lease","property",
    "lamai","ламай","бопхут","маенам","чонг мон","чавенг","bophut","maenam","choeng mon","chaweng"
}
BLOCK_PATTERNS = (
    "местных агентств","других агентств","на facebook","в группах facebook",
    "агрегаторах","marketplace","airbnb","booking","renthub","fazwaz","dotproperty",
    "list with","contact local agencies","facebook groups",
)

TYPE, BUDGET, AREA, BEDROOMS, CHECKIN, CHECKOUT, NOTES = range(7)
DUPLICATE_COOLDOWN_SEC = 15 * 60  # 15 минут

# ─────────────── УТИЛИТЫ
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")

def env_required(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"ENV {name} is required but missing")
    return val

def mentions_realty(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in REALTY_KEYWORDS)

def looks_like_realty_question(text: str) -> bool:
    return mentions_realty(text or "")

def sanitize_competitors(text: str) -> str:
    if not text:
        return text
    low = text.lower()
    if any(p in low for p in BLOCK_PATTERNS):
        msg, _ = build_cta_public()
        return "Чтобы не тратить время на сторонние площадки, лучше сразу к нам.\n\n" + msg
    return text

def parse_to_iso_date(text: str) -> str:
    """Любые привычные форматы → YYYY-MM-DD; если не вышло — возвращаем как есть."""
    s = (text or "").strip()
    if not s: return s
    try:
        dt = dtparser.parse(s, dayfirst=True,  yearfirst=False, fuzzy=True)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        try:
            dt = dtparser.parse(s, dayfirst=False, yearfirst=True,  fuzzy=True)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return s

# ─────────────── CTA/КНОПКИ
def build_cta_public() -> Tuple[str, InlineKeyboardMarkup]:
    kb = [
        [InlineKeyboardButton("🌐 Открыть сайт", url=WEBSITE_URL)],
        [InlineKeyboardButton("📣 Телеграм-канал (все лоты)", url=TG_CHANNEL_MAIN)],
        [InlineKeyboardButton("🏡 Канал по виллам", url=TG_CHANNEL_VILLAS)],
        [InlineKeyboardButton("📷 Instagram", url=INSTAGRAM_URL)],
    ]
    msg = (
        "🏝️ По недвижимости лучше сразу у нас:\n"
        f"• Сайт: {WEBSITE_URL}\n"
        f"• Канал с лотами: {TG_CHANNEL_MAIN}\n"
        f"• Канал по виллам: {TG_CHANNEL_VILLAS}\n"
        f"• Instagram: {INSTAGRAM_URL}\n\n"
        "✍️ Самый действенный способ — пройти короткую анкету /rent.\n"
        "Я сделаю подборку лотов по вашим критериям и передам менеджеру."
    )
    return msg, InlineKeyboardMarkup(kb)

def build_cta_with_manager() -> Tuple[str, InlineKeyboardMarkup]:
    msg, kb = build_cta_public()
    kb.inline_keyboard.append([InlineKeyboardButton("👤 Написать менеджеру", url=MANAGER_TG_URL)])
    msg += "\n\n👤 Контакт менеджера открыт ниже."
    return msg, kb

# ─────────────── Подборки по каналам (deep search)
def build_channel_search_links(area: str, bedrooms: str, budget: str) -> List[Tuple[str, str]]:
    q = " ".join(x for x in [area, f"{bedrooms} спальн" if bedrooms else "", budget] if x).strip()
    qenc = urllib.parse.quote(q) if q else ""
    pairs: List[Tuple[str, str]] = []
    if MAIN_CH_USERNAME:
        pairs.append((f"Подборка в {MAIN_CH_USERNAME}", f"https://t.me/s/{MAIN_CH_USERNAME}?q={qenc}"))
    if VILLAS_CH_USERNAME:
        pairs.append((f"Подборка в {VILLAS_CH_USERNAME}", f"https://t.me/s/{VILLAS_CH_USERNAME}?q={qenc}"))
    return pairs

def format_links_md(pairs: List[Tuple[str,str]]) -> str:
    if not pairs: return "—"
    return "\n".join([f"• {title}: {url}" for title, url in pairs])

# ─────────────── МОДЕЛЬ ЗАЯВКИ
@dataclass
class Lead:
    created_at: str
    user_id: str
    username: str
    first_name: str
    type: str
    area: str
    budget: str
    bedrooms: str
    checkin: str
    checkout: str
    notes: str
    source: str = "telegram_bot"

    @classmethod
    def from_context(cls, update: Update, form: dict) -> "Lead":
        return cls(
            created_at=now_str(),
            user_id=str(update.effective_user.id),
            username=update.effective_user.username or "",
            first_name=update.effective_user.first_name or "",
            type=form.get("type",""),
            area=form.get("area",""),
            budget=form.get("budget",""),
            bedrooms=form.get("bedrooms",""),
            checkin=form.get("checkin",""),
            checkout=form.get("checkout",""),
            notes=form.get("notes",""),
        )

    def as_row(self) -> list[str]:
        return [
            self.created_at, self.user_id, self.username, self.first_name,
            self.type, self.area, self.budget, self.bedrooms,
            self.checkin, self.checkout, self.notes, self.source
        ]

# ─────────────── Google Sheets (опционально)
class SheetsClient:
    def __init__(self, sheet_id: Optional[str], sheet_name: str = "Leads"):
        self.sheet_id = sheet_id
        self.sheet_name = sheet_name
        self._gc = None
        self._ws = None

    def _ready(self) -> bool:
        return bool(self.sheet_id)

    def _authorize(self):
        if self._gc: return
        import gspread
        from google.oauth2.service_account import Credentials
        svc_json = env_required("GOOGLE_SERVICE_ACCOUNT_JSON")
        info = json.loads(svc_json)
        creds = Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        self._gc = gspread.authorize(creds)

    def _open_ws(self):
        if self._ws: return
        self._authorize()
        sh = self._gc.open_by_key(self.sheet_id)
        try:
            self._ws = sh.worksheet(self.sheet_name)
        except Exception:
            self._ws = sh.add_worksheet(title=self.sheet_name, rows=1000, cols=26)
        if not self._ws.get_all_values():
            self._ws.append_row(
                ["created_at","user_id","username","first_name","type","area","budget",
                 "bedrooms","checkin","checkout","notes","source","proposed_links"],
                value_input_option="USER_ENTERED"
            )

    def append_lead(self, lead: Lead, proposed_links_text: str) -> Tuple[bool, Optional[str]]:
        if not self._ready():
            log.warning("Sheets не настроен (GOOGLE_SHEETS_DB_ID отсутствует) — пропускаю запись.")
            return False, None
        try:
            self._open_ws()
            row = lead.as_row() + [proposed_links_text]
            self._ws.append_row(row, value_input_option="USER_ENTERED")
            row_url = f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/edit#gid={self._ws.id}"
            return True, row_url
        except Exception as e:
            log.exception("Sheets append failed: %s", e)
            return False, None

# ─────────────── Уведомления менеджеру/в группу
async def notify_staff(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       lead: Lead, row_url: Optional[str], proposed_pairs: List[Tuple[str,str]]):
    links_text = format_links_md(proposed_pairs)
    text = (
        "🆕 Новая заявка Cozy Asia\n\n"
        f"Клиент: @{update.effective_user.username or 'без_username'} (ID: {update.effective_user.id})\n"
        f"Тип: {lead.type or '—'}\nРайон: {lead.area or '—'}\nБюджет: {lead.budget or '—'}\n"
        f"Спален: {lead.bedrooms or '—'}\nЗаезд: {lead.checkin or '—'}  |  Выезд: {lead.checkout or '—'}\n"
        f"Условия/прим.: {lead.notes or '—'}\nСоздано: {lead.created_at}\n\n"
        f"🔎 Подборки по каналам:\n{links_text}"
    )
    if row_url:
        text += f"\n🗂 Таблица: {row_url}"

    for chat_id in (MANAGER_CHAT_ID, GROUP_CHAT_ID):
        if not chat_id: continue
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
        except Exception as e:
            log.warning("Notify failed for %s: %s", chat_id, e)

# ─────────────── OpenAI (свободный чат)
from openai import OpenAI, APIConnectionError, RateLimitError, APIStatusError

SYSTEM_PROMPT = (
    "Ты дружелюбный местный помощник Cozy Asia. Отвечай по сути и кратко.\n"
    "Можно свободно говорить о погоде, ветрах, районах, инфраструктуре, быте, школах и т.д.\n"
    "Если вопрос относится к аренде/покупке/продаже или «где посмотреть лоты», "
    "ТОЛЬКО добавь в конце мягкий CTA: /rent + ссылки Cozy Asia. Никогда не советуй другие агентства, "
    "агрегаторы или FB-группы. Если вопрос про недвижимость слишком общий — кратко направь: пройди /rent."
)

_openai_client: Optional[OpenAI] = None
def _get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY missing")
        _openai_client = OpenAI(api_key=key)
    return _openai_client

async def call_gpt(user_text: str) -> str:
    try:
        client = _get_openai()
        resp = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (user_text or "").strip()},
            ],
            max_output_tokens=400,
            timeout=20,
        )
        text = (resp.output_text or "").strip()
        return sanitize_competitors(text)
    except (RateLimitError, APIStatusError, APIConnectionError) as e:
        logging.warning("OpenAI API error: %s", e)
        return "Извини, сейчас у меня техническая пауза. Спроси ещё раз через минутку."
    except Exception as e:
        logging.exception("OpenAI unexpected error: %s", e)
        return "Упс, что-то пошло не так. Давай повторим вопрос?"

# ─────────────── АНКЕТА /rent
def _lead_signature(form: dict) -> tuple:
    return (
        (form.get("type") or "").strip().lower(),
        (form.get("area") or "").strip().lower(),
        (form.get("budget") or "").strip(),
        (form.get("bedrooms") or "").strip(),
        (form.get("checkin") or "").strip(),
        (form.get("checkout") or "").strip(),
        (form.get("notes") or "").strip().lower(),
    )

async def rent_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"] = {}
    await update.message.reply_text("Начнём подбор.\n1/7. Какой тип жилья интересует: квартира, дом или вилла?")
    return TYPE

async def rent_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["type"] = (update.message.text or "").strip()
    await update.message.reply_text("2/7. Какой у вас бюджет в батах (месяц)?")
    return BUDGET

async def rent_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["budget"] = (update.message.text or "").strip()
    await update.message.reply_text("3/7. В каком районе Самуи предпочтительно жить?")
    return AREA

async def rent_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["area"] = (update.message.text or "").strip()
    await update.message.reply_text("4/7. Сколько нужно спален?")
    return BEDROOMS

async def rent_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["bedrooms"] = (update.message.text or "").strip()
    await update.message.reply_text("5/7. Дата заезда?")
    return CHECKIN

async def rent_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val_raw = (update.message.text or "").strip()
    context.user_data["form"]["checkin"] = parse_to_iso_date(val_raw)
    await update.message.reply_text("6/7. Дата выезда?")
    return CHECKOUT

async def rent_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val_raw = (update.message.text or "").strip()
    context.user_data["form"]["checkout"] = parse_to_iso_date(val_raw)
    await update.message.reply_text("7/7. Важные условия? (близость к пляжу, с питомцами, парковка и т.п.)")
    return NOTES

async def rent_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["notes"] = (update.message.text or "").strip()
    form = context.user_data["form"]
    lead = Lead.from_context(update, form)

    # Подборки по каналам
    proposed_pairs = build_channel_search_links(form.get("area",""), form.get("bedrooms",""), form.get("budget",""))
    proposed_text_for_sheet = format_links_md(proposed_pairs)
    proposed_count = len(proposed_pairs)

    # Анти-дубликаты (15 минут)
    sig = _lead_signature(form)
    last_leads = context.application.bot_data.get("last_leads", {})
    now_ts = int(time.time())
    entry = last_leads.get(update.effective_user.id)
    is_duplicate = bool(entry and entry[0] == sig and (now_ts - entry[1]) < DUPLICATE_COOLDOWN_SEC)

    row_url = None
    if not is_duplicate:
        sheets: SheetsClient = context.application.bot_data.get("sheets")
        if isinstance(sheets, SheetsClient):
            ok, row_url = sheets.append_lead(lead, proposed_text_for_sheet)
            if not ok:
                log.error("Не удалось записать в Google Sheets")
        await notify_staff(update, context, lead, row_url=row_url, proposed_pairs=proposed_pairs)
        last_leads[update.effective_user.id] = (sig, now_ts)
        context.application.bot_data["last_leads"] = last_leads

    # Флаг — форму прошёл (после этого новые заявки автоматически не создаются)
    context.user_data["rental_form_completed"] = True

    # Ответ пользователю
    human_links = "\n".join([f"• {t}: {u}" for t,u in proposed_pairs]) or "—"
    msg_user, kb = build_cta_with_manager()
    msg_user = (
        "Заявка сформирована ✅ и уже передана менеджеру.\n"
        f"🔎 По вашим параметрам нашёл подборки ({proposed_count}):\n{human_links}\n\n"
        + msg_user +
        "\n\n✉️ Если понадобится уточнить детали — просто напишите мне, отвечу как обычный чат."
    )
    await update.message.reply_text(msg_user, reply_markup=kb, disable_web_page_preview=True)
    return ConversationHandler.END

async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Окей, если передумаете — пишите /rent.")
    return ConversationHandler.END

# ─────────────── Свободный чат (GPT + умный CTA)
async def free_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    1) Всегда отвечаем GPT на любой текст.
    2) Если тема затрагивает недвижимость — ДОБАВЛЯЕМ наш CTA (а не заменяем ответ).
    3) После прохождения анкеты показываем кнопку менеджера, до — без неё.
    """
    user_text = update.effective_message.text or ""
    completed = bool(context.user_data.get("rental_form_completed", False))

    # Ответ GPT
    gpt_reply = (await call_gpt(user_text)).strip()

    # Нужно ли прикрепить CTA?
    need_cta = looks_like_realty_question(user_text) or looks_like_realty_question(gpt_reply)

    if need_cta:
        cta_msg, cta_kb = (build_cta_with_manager() if completed else build_cta_public())
        tail = (
            "\n\n🔧 Самый действенный способ — пройти короткую анкету командой /rent.\n"
            "Я сделаю подборку лотов (дома/апартаменты/виллы) по вашим критериям и сразу отправлю вам.\n"
            f"{'Менеджер уже в курсе и свяжется с вами в ближайшее время.' if completed else 'Менеджер получит вашу заявку и свяжется для уточнений.'}"
        )
        final_text = f"{gpt_reply}{tail}\n\n{cta_msg}"
        await update.effective_message.reply_text(final_text, reply_markup=cta_kb, disable_web_page_preview=True)
        return

    # Не про недвижимость — чистый ответ модели
    await update.effective_message.reply_text(gpt_reply, disable_web_page_preview=True)

# ─────────────── Служебные команды
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш Chat ID: {update.effective_chat.id}\nВаш User ID: {update.effective_user.id}"
    )

async def cmd_groupid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Group chat ID: {update.effective_chat.id}")

async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    openai_set = bool(os.getenv("OPENAI_API_KEY"))
    sheets_id = os.getenv("GOOGLE_SHEETS_DB_ID") or "—"
    group_id  = os.getenv("GROUP_CHAT_ID") or str(GROUP_CHAT_ID or "—")
    txt = (
        "🔎 Диагностика:\n"
        f"• OPENAI_API_KEY: {'OK' if openai_set else 'MISSING'}\n"
        f"• GOOGLE_SHEETS_DB_ID: {sheets_id}\n"
        f"• GROUP_CHAT_ID: {group_id}\n"
        f"• WEBHOOK_BASE: {os.getenv('WEBHOOK_BASE') or os.getenv('RENDER_EXTERNAL_URL') or '—'}"
    )
    await update.message.reply_text(txt)

# ─────────────── Webhook utils
def preflight_release_webhook(token: str):
    base = f"https://api.telegram.org/bot{token}"
    try:
        requests.post(f"{base}/deleteWebhook", params={"drop_pending_updates": True}, timeout=10)
        log.info("deleteWebhook -> OK")
    except Exception as e:
        log.warning("deleteWebhook error: %s", e)

# ─────────────── Bootstrap
def build_application() -> Application:
    token = env_required("TELEGRAM_BOT_TOKEN")

    # Sheets (опционально)
    sheet_id  = os.getenv("GOOGLE_SHEETS_DB_ID")
    sheet_name = os.getenv("GOOGLE_SHEETS_SHEET_NAME", "Leads")
    sheets = SheetsClient(sheet_id=sheet_id, sheet_name=sheet_name)

    app = ApplicationBuilder().token(token).build()
    app.bot_data["sheets"] = sheets
    app.bot_data["last_leads"] = {}

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("groupid", cmd_groupid))
    app.add_handler(CommandHandler("diag", cmd_diag))

    # Анкета /rent
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_start)],
        states={
            TYPE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_type)],
            BUDGET:   [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_budget)],
            AREA:     [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_area)],
            BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_bedrooms)],
            CHECKIN:  [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_checkin)],
            CHECKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_checkout)],
            NOTES:    [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_finish)],
        },
        fallbacks=[CommandHandler("cancel", rent_cancel)],
    )
    app.add_handler(conv)

    # ВАЖНО: никаких других “заглушек” выше — свободный чат всегда в конце.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_handler))
    return app

def main():
    token = env_required("TELEGRAM_BOT_TOKEN")
    base_url = os.getenv("WEBHOOK_BASE") or os.getenv("RENDER_EXTERNAL_URL")
    if not base_url:
        raise RuntimeError("WEBHOOK_BASE (или RENDER_EXTERNAL_URL) не задан.")
    preflight_release_webhook(token)

    app = build_application()

    port = int(os.getenv("PORT", "10000"))
    url_path = token  # токен не светим в URL браузера, только внутри Render
    webhook_url = f"{base_url.rstrip('/')}/webhook/{url_path}"

    log.info("Starting webhook on 0.0.0.0:%s | url=%s", port, webhook_url)
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=f"webhook/{url_path}",
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()
