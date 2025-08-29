# main.py — Cozy Asia Bot (ptb v20+, webhook/Render)
# -----------------------------------------------------------------------------
# Функционал:
# - /start (новое приветствие)
# - /rent анкета: type → budget → area → bedrooms → checkin → checkout → notes
#   -> запись в Google Sheets (если настроено)
#   -> автоподбор ссылок на ваши каналы по запросу (deep search)
#   -> уведомления менеджеру и в рабочую группу (+ссылка на шит, +ссылки-подборки)
# - Свободный чат через OpenAI: общение на любые темы, но про недвижимость
#   — мягко ведём к /rent и вашим ресурсам; без рекламы других агентств
# - Служебные: /id /groupid /diag
# - Webhook для Render: 0.0.0.0:$PORT, URL = WEBHOOK_BASE/webhook/<BOT_TOKEN>

from __future__ import annotations
import os, json, logging, time, urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

import requests
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

# ────────────────────────── ЛОГИ
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("cozyasia-bot")

# ────────────────────────── ВАШИ ССЫЛКИ/КОНТАКТЫ
WEBSITE_URL       = "https://www.cozy-asiath.com/"
TG_CHANNEL_MAIN   = "https://t.me/SamuiRental"
TG_CHANNEL_VILLAS = "https://t.me/arenda_vill_samui"
INSTAGRAM_URL     = "https://www.instagram.com/cozy.asia?igsh=cmt1MHA0ZmM3OTRu"
MAIN_CH_USERNAME  = "SamuiRental"
VILLAS_CH_USERNAME= "arenda_vill_samui"

MANAGER_TG_URL  = "https://t.me/cozy_asia"   # показываем ТОЛЬКО после анкеты
MANAGER_CHAT_ID = 5978240436                 # личка менеджера

GROUP_CHAT_ID: Optional[int] = None          # рабочая группа
_env_group = os.getenv("GROUP_CHAT_ID")
if _env_group:
    try:
        GROUP_CHAT_ID = int(_env_group)
    except Exception:
        log.warning("GROUP_CHAT_ID из ENV не int: %r", _env_group)

# ────────────────────────── ТЕКСТЫ
START_TEXT = (
    "✅ Я уже тут!\n"
    "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n"
    "👉 Или нажмите команду /rent — я задам несколько вопросов о жилье, "
    "сформирую заявку, предложу варианты и передам менеджеру.\n"
    "Он свяжется с вами для уточнения деталей и бронирования."
)

REALTY_KEYWORDS = {
    "аренда","сдать","сниму","снять","дом","вилла","квартира","комнаты","спальни",
    "покупка","купить","продажа","продать","недвижимость","кондо","condo","таунхаус",
    "bungalow","bungalo","house","villa","apartment","rent","buy","sale","lease","property",
    "lamai","ламай","бопхут","маенам","чонг мон","чавенг","bophut","maenam","choeng mon","chaweng"
}

BLOCK_PATTERNS = (
    "местных агентств","других агентств","на facebook","в группах facebook",
    "агрегаторах","marketplace","airbnb","booking","renthub","fazwaz",
    "dotproperty","list with","contact local agencies","facebook groups",
)

TYPE, BUDGET, AREA, BEDROOMS, CHECKIN, CHECKOUT, NOTES = range(7)
DEFAULT_PORT = 10000

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

def sanitize_competitors(text: str) -> str:
    if not text:
        return text
    low = text.lower()
    if any(p in low for p in BLOCK_PATTERNS):
        msg, _ = build_cta_public()
        return "Чтобы не тратить время на сторонние площадки, лучше сразу к нам.\n\n" + msg
    return text

# ────────────────────────── CTA
def build_cta_public() -> tuple[str, InlineKeyboardMarkup]:
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
        "✍️ Связаться с менеджером можно после короткой заявки в /rent — "
        "это нужно, чтобы зафиксировать запрос и выдать точные варианты."
    )
    return msg, InlineKeyboardMarkup(kb)

def build_cta_with_manager() -> tuple[str, InlineKeyboardMarkup]:
    msg, kb = build_cta_public()
    kb.inline_keyboard.append([InlineKeyboardButton("👤 Написать менеджеру", url=MANAGER_TG_URL)])
    msg += "\n\n👤 Контакт менеджера открыт ниже."
    return msg, kb

# ────────────────────────── Дип-поиск по каналам (без чтения сообщений)
def build_channel_search_links(area: str, bedrooms: str, budget: str) -> list[tuple[str, str]]:
    q = " ".join(x for x in [area, f"{bedrooms} спальн" if bedrooms else "", budget] if x).strip()
    qenc = urllib.parse.quote(q) if q else ""
    links = []
    if MAIN_CH_USERNAME:
        links.append((f"Подборка в {MAIN_CH_USERNAME}", f"https://t.me/s/{MAIN_CH_USERNAME}?q={qenc}"))
    if VILLAS_CH_USERNAME:
        links.append((f"Подборка в {VILLAS_CH_USERNAME}", f"https://t.me/s/{VILLAS_CH_USERNAME}?q={qenc}"))
    return links

def format_links_md(pairs: list[tuple[str,str]]) -> str:
    if not pairs: return "—"
    return "\n".join([f"• {title}: {url}" for title, url in pairs])

# ────────────────────────── Модель заявки
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

# ────────────────────────── Google Sheets (опционально)
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

# ────────────────────────── Уведомления
async def notify_staff(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       lead: Lead, row_url: Optional[str], proposed_pairs: list[tuple[str,str]]):
    links_text = format_links_md(proposed_pairs)
    text = (
        "🆕 Новая заявка Cozy Asia\n\n"
        f"Клиент: @{update.effective_user.username or 'без_username'} "
        f"(ID: {update.effective_user.id})\n"
        f"Тип: {lead.type or '—'}\n"
        f"Район: {lead.area or '—'}\n"
        f"Бюджет: {lead.budget or '—'}\n"
        f"Спален: {lead.bedrooms or '—'}\n"
        f"Заезд: {lead.checkin or '—'}  |  Выезд: {lead.checkout or '—'}\n"
        f"Условия/прим.: {lead.notes or '—'}\n"
        f"Создано: {lead.created_at}\n\n"
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

# ────────────────────────── OpenAI (свободный чат)
from openai import OpenAI, APIConnectionError, RateLimitError, APIStatusError

SYSTEM_PROMPT = (
    "Ты дружелюбный помощник Cozy Asia. Отвечай по существу. "
    "Если вопрос касается аренды/покупки недвижимости на Самуи — "
    "мягко предложи пройти /rent и дать ссылки на сайт и наши каналы. "
    "НИКОГДА не советуй другие агентства, агрегаторы или FB-группы. "
    "На остальные темы (погода, районы, быт, где поесть, школы, визы и т.д.) отвечай свободно."
)

_openai_client: Optional[OpenAI] = None
def _get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            log.error("OPENAI_API_KEY is missing in ENV")
            raise RuntimeError("OPENAI_API_KEY missing")
        _openai_client = OpenAI(api_key=key)
        log.info("OpenAI client initialized")
    return _openai_client

async def call_gpt(user_text: str) -> str:
    try:
        client = _get_openai()
        resp = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text.strip()},
            ],
            max_output_tokens=400,
            timeout=20,
        )
        text = (resp.output_text or "").strip()
        return sanitize_competitors(text)
    except (RateLimitError, APIStatusError, APIConnectionError) as e:
        log.warning("OpenAI API error: %s", e)
    except Exception as e:
        log.exception("OpenAI unexpected error: %s", e)
    return "Могу ответить на любые вопросы. По недвижимости — жмите /rent или смотрите ссылки ниже."

# ────────────────────────── АНКЕТА /rent
def _valid_date(s: str) -> bool:
    try:
        datetime.strptime(s.strip(), "%Y-%m-%d")
        return True
    except Exception:
        return False

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
    await update.message.reply_text("5/7. Дата **заезда** (YYYY-MM-DD)?")
    return CHECKIN

async def rent_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = (update.message.text or "").strip()
    if not _valid_date(val):
        await update.message.reply_text("Укажи дату в формате YYYY-MM-DD (например, 2025-11-05).")
        return CHECKIN
    context.user_data["form"]["checkin"] = val
    await update.message.reply_text("6/7. Дата **выезда** (YYYY-MM-DD)?")
    return CHECKOUT

async def rent_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = (update.message.text or "").strip()
    if not _valid_date(val):
        await update.message.reply_text("Укажи дату в формате YYYY-MM-DD (например, 2025-12-03).")
        return CHECKOUT
    context.user_data["form"]["checkout"] = val
    await update.message.reply_text("7/7. Важные условия? (близость к пляжу, с питомцами, парковка и т.п.)")
    return NOTES

async def rent_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["notes"] = (update.message.text or "").strip()
    form = context.user_data["form"]
    lead = Lead.from_context(update, form)

    # Подборки по вашим каналам (deep search)
    proposed_pairs = build_channel_search_links(form.get("area",""), form.get("bedrooms",""), form.get("budget",""))
    proposed_text_for_sheet = format_links_md(proposed_pairs)

    # Запись в Sheets (если настроено)
    sheets: SheetsClient = context.application.bot_data.get("sheets")
    row_url = None
    if isinstance(sheets, SheetsClient):
        ok, row_url = sheets.append_lead(lead, proposed_text_for_sheet)
        if not ok:
            log.error("Не удалось записать в Google Sheets")

    # Отметка — форму прошёл (открываем менеджера в CTA)
    context.user_data["rental_form_completed"] = True

    # Уведомления менеджеру и в группу (с ссылками и шитом)
    await notify_staff(update, context, lead, row_url=row_url, proposed_pairs=proposed_pairs)

    # Пользователю — ссылки + менеджер
    human_links = "\n".join([f"• {t}: {u}" for t,u in proposed_pairs]) or "—"
    msg_user, kb = build_cta_with_manager()
    msg_user = (
        "Заявка сохранена ✅\n\n"
        "🔎 Я уже посмотрел, что есть на наших каналах. Вот подборки по вашему запросу:\n"
        f"{human_links}\n\n" + msg_user
    )
    await update.message.reply_text(msg_user, reply_markup=kb, disable_web_page_preview=True)
    return ConversationHandler.END

async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Окей, если передумаете — пишите /rent.")
    return ConversationHandler.END

# ────────────────────────── Свободный чат
async def free_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text or ""
    completed = bool(context.user_data.get("rental_form_completed", False))

    # Только если явно «недвижимость» — ведём в нашу воронку
    if mentions_realty(text):
        msg, kb = (build_cta_with_manager() if completed else build_cta_public())
        await update.effective_message.reply_text(msg, reply_markup=kb, disable_web_page_preview=True)
        return

    # Любые НЕДЕВЕЖИМОСТНЫЕ вопросы — нормальный GPT-ответ
    reply = await call_gpt(text)
    await update.effective_message.reply_text(reply, disable_web_page_preview=True)

# ────────────────────────── Служебные команды
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
    group_id = os.getenv("GROUP_CHAT_ID") or str(GROUP_CHAT_ID or "—")
    txt = (
        "🔎 Диагностика:\n"
        f"• OPENAI_API_KEY: {'OK' if openai_set else 'MISSING'}\n"
        f"• GOOGLE_SHEETS_DB_ID: {sheets_id}\n"
        f"• GROUP_CHAT_ID: {group_id}\n"
        f"• WEBHOOK_BASE: {os.getenv('WEBHOOK_BASE') or os.getenv('RENDER_EXTERNAL_URL') or '—'}"
    )
    await update.message.reply_text(txt)

# ────────────────────────── Webhook utils
def preflight_release_webhook(token: str):
    base = f"https://api.telegram.org/bot{token}"
    try:
        requests.post(f"{base}/deleteWebhook", params={"drop_pending_updates": True}, timeout=10)
        log.info("deleteWebhook -> OK")
    except Exception as e:
        log.warning("deleteWebhook error: %s", e)

# ────────────────────────── Bootstrap
def build_application() -> Application:
    token = env_required("TELEGRAM_BOT_TOKEN")

    sheet_id = os.getenv("GOOGLE_SHEETS_DB_ID")
    sheet_name = os.getenv("GOOGLE_SHEETS_SHEET_NAME", "Leads")
    sheets = SheetsClient(sheet_id=sheet_id, sheet_name=sheet_name)

    app = ApplicationBuilder().token(token).build()
    app.bot_data["sheets"] = sheets

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("groupid", cmd_groupid))
    app.add_handler(CommandHandler("diag", cmd_diag))

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

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_handler))
    return app

def main():
    token = env_required("TELEGRAM_BOT_TOKEN")
    base_url = os.getenv("WEBHOOK_BASE") or os.getenv("RENDER_EXTERNAL_URL")
    if not base_url:
        raise RuntimeError("WEBHOOK_BASE (или RENDER_EXTERNAL_URL) не задан.")
    preflight_release_webhook(token)

    app = build_application()

    port = int(os.getenv("PORT", str(DEFAULT_PORT)))
    url_path = token
    webhook_url = f"{base_url.rstrip('/')}/webhook/{url_path}"

    log.info("Starting webhook on 0.0.0.0:%s | url=%s", port, webhook_url)
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=f"webhook/{url_path}",
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
