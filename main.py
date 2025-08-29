# main.py — Cozy Asia Bot (ptb v20+, WEBHOOK для Render Web Service)
# ─────────────────────────────────────────────────────────────────────────────
# Что внутри:
# - /start с новым приветствием
# - /rent анкета → (опционально) Google Sheets → уведомления менеджеру и в группу
# - Контакт менеджера показываем ТОЛЬКО ПОСЛЕ анкеты
# - Перехват любых упоминаний недвижимости → ведём на наши ресурсы
# - Свободный чат через OpenAI Responses API (ENV OPENAI_API_KEY)
# - Никогда не советуем другие агентства/агрегаторы/FB-группы (двойной контроль)
# - /id и /groupid для получения Chat ID
# - Webhook: 0.0.0.0:$PORT, URL = WEBHOOK_BASE/webhook/<BOT_TOKEN>

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ───────────────────────────────────── ЛОГИ
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("cozyasia-bot")

# ─────────────────────────────── БРЕНД/ССЫЛКИ
WEBSITE_URL       = "https://www.cozy-asiath.com/"
TG_CHANNEL_MAIN   = "https://t.me/SamuiRental"
TG_CHANNEL_VILLAS = "https://t.me/arenda_vill_samui"
INSTAGRAM_URL     = "https://www.instagram.com/cozy.asia?igsh=cmt1MHA0ZmM3OTRu"

# Менеджер (контакт показываем ТОЛЬКО ПОСЛЕ анкеты)
MANAGER_TG_URL  = "https://t.me/cozy_asia"   # @Cozy_asia
MANAGER_CHAT_ID = 5978240436                 # личка менеджера

# Рабочая группа (опционально из ENV)
GROUP_CHAT_ID: Optional[int] = None
_env_group = os.getenv("GROUP_CHAT_ID")
if _env_group:
    try:
        GROUP_CHAT_ID = int(_env_group)
    except Exception:
        log.warning("GROUP_CHAT_ID из ENV не int: %r", _env_group)

# ─────────────────────────────── Приветствие
START_TEXT = (
    "✅ Я уже тут!\n"
    "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n"
    "👉 Или нажмите команду /rent — я задам несколько вопросов о жилье, "
    "сформирую заявку, предложу варианты и передам менеджеру.\n"
    "Он свяжется с вами для уточнения деталей и бронирования."
)

# ─────────────────────────────── Триггеры «недвижимости»
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

TYPE, BUDGET, AREA, BEDROOMS, NOTES = range(5)  # состояния анкеты
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

# ─────────────────────────────── CTA
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
        "✍️ Связаться с менеджером можно после короткой заявки в /rent — "
        "это нужно, чтобы зафиксировать запрос и выдать точные варианты."
    )
    return msg, InlineKeyboardMarkup(kb)

def build_cta_with_manager() -> Tuple[str, InlineKeyboardMarkup]:
    msg, kb = build_cta_public()
    kb.inline_keyboard.append([InlineKeyboardButton("👤 Написать менеджеру", url=MANAGER_TG_URL)])
    msg += "\n\n👤 Контакт менеджера открыт ниже."
    return msg, kb

# ─────────────────────────────── Модель заявки
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
            notes=form.get("notes",""),
        )

    def as_row(self) -> list[str]:
        return [
            self.created_at, self.user_id, self.username, self.first_name,
            self.type, self.area, self.budget, self.bedrooms, self.notes, self.source
        ]

# ─────────────────────────────── Google Sheets (опционально)
class SheetsClient:
    def __init__(self, sheet_id: Optional[str], sheet_name: str = "Leads"):
        self.sheet_id = sheet_id
        self.sheet_name = sheet_name
        self._gc = None
        self._ws = None

    def _ready(self) -> bool:
        return bool(self.sheet_id)

    def _authorize(self):
        if self._gc:
            return
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
        if self._ws:
            return
        self._authorize()
        sh = self._gc.open_by_key(self.sheet_id)
        try:
            self._ws = sh.worksheet(self.sheet_name)
        except Exception:
            self._ws = sh.add_worksheet(title=self.sheet_name, rows=1000, cols=20)
        if not self._ws.get_all_values():
            self._ws.append_row(
                ["created_at","user_id","username","first_name","type","area","budget","bedrooms","notes","source"],
                value_input_option="USER_ENTERED"
            )

    def append_lead(self, lead: Lead) -> Tuple[bool, Optional[str]]:
        if not self._ready():
            log.warning("Sheets не настроен (GOOGLE_SHEETS_DB_ID отсутствует) — пропускаю запись.")
            return False, None
        try:
            self._open_ws()
            self._ws.append_row(lead.as_row(), value_input_option="USER_ENTERED")
            row_url = f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/edit#gid={self._ws.id}"
            return True, row_url
        except Exception as e:
            log.exception("Sheets append failed: %s", e)
            return False, None

# ─────────────────────────────── Уведомления
async def notify_staff(update: Update, context: ContextTypes.DEFAULT_TYPE, lead: Lead, row_url: Optional[str]):
    text = (
        "🆕 Новая заявка Cozy Asia\n\n"
        f"Клиент: @{update.effective_user.username or 'без_username'} "
        f"(ID: {update.effective_user.id})\n"
        f"Тип: {lead.type or '—'}\n"
        f"Район: {lead.area or '—'}\n"
        f"Бюджет: {lead.budget or '—'}\n"
        f"Спален: {lead.bedrooms or '—'}\n"
        f"Условия/прим.: {lead.notes or '—'}\n"
        f"Создано: {lead.created_at}"
    )
    if row_url:
        text += f"\n🗂 Таблица: {row_url}"

    for chat_id in (MANAGER_CHAT_ID, GROUP_CHAT_ID):
        if not chat_id:
            continue
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
        except Exception as e:
            log.warning("Notify failed for %s: %s", chat_id, e)

# ─────────────────────────────── OpenAI (свободный чат)
# pip: openai>=1.40.0
from openai import OpenAI
SYSTEM_PROMPT = (
    "Ты дружелюбный помощник Cozy Asia. Отвечай кратко и по делу. "
    "НИКОГДА не советуй другие агентства, агрегаторы (Airbnb, Booking, FazWaz и т.п.) "
    "и FB-группы. Если запрос связан с недвижимостью на Самуи, мягко направляй на /rent "
    "и на наши ссылки (сайт/каналы/Instagram). На любые другие темы отвечай нормально."
)
_openai_client: Optional[OpenAI] = None
def _get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        # ключ берётся из ENV OPENAI_API_KEY
        _openai_client = OpenAI()
    return _openai_client

async def call_gpt(user_text: str) -> str:
    try:
        client = _get_openai()
        resp = client.responses.create(
            model="gpt-4o",
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text.strip()},
            ],
            max_output_tokens=400,
        )
        text = (resp.output_text or "").strip()
        return sanitize_competitors(text)
    except Exception as e:
        log.warning("OpenAI error: %s", e)
        return "Могу ответить на любые вопросы. По недвижимости — жмите /rent или смотрите ссылки ниже."

# ─────────────────────────────── Анкета /rent
async def rent_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"] = {}
    await update.message.reply_text("Начнём подбор.\n1/5. Какой тип жилья интересует: квартира, дом или вилла?")
    return TYPE

async def rent_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["type"] = (update.message.text or "").strip()
    await update.message.reply_text("2/5. Какой у вас бюджет в батах (месяц)?")
    return BUDGET

async def rent_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["budget"] = (update.message.text or "").strip()
    await update.message.reply_text("3/5. В каком районе Самуи предпочтительно жить?")
    return AREA

async def rent_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["area"] = (update.message.text or "").strip()
    await update.message.reply_text("4/5. Сколько нужно спален?")
    return BEDROOMS

async def rent_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["bedrooms"] = (update.message.text or "").strip()
    await update.message.reply_text("5/5. Важные условия? (близость к пляжу, с питомцами, парковка и т.п.)")
    return NOTES

async def rent_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["notes"] = (update.message.text or "").strip()
    form = context.user_data["form"]
    lead = Lead.from_context(update, form)

    # Запись в Sheets (если настроен sheet_id)
    sheets = context.application.bot_data.get("sheets")  # мы положим ниже
    row_url = None
    if isinstance(sheets, SheetsClient):
        ok, row_url = sheets.append_lead(lead)
        if not ok:
            log.error("Не удалось записать в Google Sheets")

    context.user_data["rental_form_completed"] = True
    await notify_staff(update, context, lead, row_url)

    msg, kb = build_cta_with_manager()
    await update.message.reply_text("Заявка сохранена ✅\n" + msg,
                                    reply_markup=kb, disable_web_page_preview=True)
    return ConversationHandler.END

async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Окей, если передумаете — пишите /rent.")
    return ConversationHandler.END

# ─────────────────────────────── Свободный чат
async def free_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text or ""
    completed = bool(context.user_data.get("rental_form_completed", False))

    if mentions_realty(text):
        msg, kb = (build_cta_with_manager() if completed else build_cta_public())
        await update.effective_message.reply_text(msg, reply_markup=kb, disable_web_page_preview=True)
        return

    reply = await call_gpt(text)
    await update.effective_message.reply_text(reply, disable_web_page_preview=True)

# ─────────────────────────────── Служебные команды
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш Chat ID: {update.effective_chat.id}\nВаш User ID: {update.effective_user.id}"
    )

async def cmd_groupid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Group chat ID: {update.effective_chat.id}")

# ─────────────────────────────── Webhook utils
def preflight_release_webhook(token: str):
    base = f"https://api.telegram.org/bot{token}"
    try:
        requests.post(f"{base}/deleteWebhook", params={"drop_pending_updates": True}, timeout=10)
        log.info("deleteWebhook -> OK")
    except Exception as e:
        log.warning("deleteWebhook error: %s", e)

# ─────────────────────────────── Bootstrap
def build_application() -> Application:
    token = env_required("TELEGRAM_BOT_TOKEN")

    # Sheets опционально — если нет ID, всё равно работаем
    sheet_id = os.getenv("GOOGLE_SHEETS_DB_ID")
    sheet_name = os.getenv("GOOGLE_SHEETS_SHEET_NAME", "Leads")
    sheets = SheetsClient(sheet_id=sheet_id, sheet_name=sheet_name)

    app = ApplicationBuilder().token(token).build()
    app.bot_data["sheets"] = sheets  # доступ из rent_finish

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("groupid", cmd_groupid))

    # Анкета /rent
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", rent_start)],
        states={
            TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_type)],
            BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_budget)],
            AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_area)],
            BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_bedrooms)],
            NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, rent_finish)],
        },
        fallbacks=[CommandHandler("cancel", rent_cancel)],
    )
    app.add_handler(conv)

    # Свободный чат
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_handler))

    return app

def main():
    token = env_required("TELEGRAM_BOT_TOKEN")
    base_url = os.getenv("WEBHOOK_BASE") or os.getenv("RENDER_EXTERNAL_URL")
    if not base_url:
        raise RuntimeError("WEBHOOK_BASE (или RENDER_EXTERNAL_URL) не задан. Укажи публичный https URL Render-сервиса.")

    preflight_release_webhook(token)

    app = build_application()

    port = int(os.getenv("PORT", str(DEFAULT_PORT)))  # Render даёт $PORT
    url_path = token                                  # секретный путь = токен
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
