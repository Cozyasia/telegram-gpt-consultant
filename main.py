# main.py — Cozy Asia Bot (python-telegram-bot v20+, WEBHOOK/Render)
# ============================================================================
# Содержимое:
#   0) Импорты и глобальная настройка логов
#   1) Константы/настройки + helpers
#   2) Модель заявки и утилиты сериализации
#   3) Клиент Google Sheets с ретраями
#   4) Нотификатор (личка менеджера + рабочая группа)
#   5) Тексты/кнопки/CTA и «санитария» ответов (без конкурентов)
#   6) Анкета /rent (ConversationHandler)
#   7) Свободный чат + триггеры «недвижимости»
#   8) Служебные команды /start /id /groupid
#   9) Webhook-режим (Render): биндинг на $PORT и URL WEBHOOK_BASE/webhook/<TOKEN>
#  10) Main bootstrap + защита от двойного запуска
# ============================================================================
# Требуемые ENV:
#   TELEGRAM_BOT_TOKEN                — токен бота
#   WEBHOOK_BASE                      — публичный https URL Render-сервиса
#   GOOGLE_SERVICE_ACCOUNT_JSON       — JSON сервис-аккаунта (в одну строку)
#   GOOGLE_SHEETS_DB_ID               — ID таблицы
#   GOOGLE_SHEETS_SHEET_NAME          — (опц.) имя листа (по умолчанию "Leads")
#   GROUP_CHAT_ID                     — (опц.) ID рабочей группы (-100…)
#
# requirements.txt (минимум):
#   python-telegram-bot>=20.7
#   gspread>=6.0.0
#   google-auth>=2.31.0
#   requests>=2.32.0
# ============================================================================

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ----------------------------------------------------------------------------
# 0) ЛОГИ
# ----------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cozyasia-bot")

# ----------------------------------------------------------------------------
# 1) КОНСТАНТЫ/НАСТРОЙКИ
# ----------------------------------------------------------------------------

# ——— Бренд/ссылки (наши ресурсы)
WEBSITE_URL: str = "https://www.cozy-asiath.com/"
TG_CHANNEL_MAIN: str = "https://t.me/SamuiRental"
TG_CHANNEL_VILLAS: str = "https://t.me/arenda_vill_samui"
INSTAGRAM_URL: str = "https://www.instagram.com/cozy.asia?igsh=cmt1MHA0ZmM3OTRu"

# ——— Менеджер: ссылку показываем ТОЛЬКО ПОСЛЕ анкеты
MANAGER_TG_URL: str = "https://t.me/cozy_asia"  # @Cozy_asia
MANAGER_CHAT_ID: int = 5978240436               # личка менеджера

# ——— Рабочая группа (опционально). Можно задать ENV GROUP_CHAT_ID=-100…
GROUP_CHAT_ID: Optional[int] = None
if os.getenv("GROUP_CHAT_ID"):
    try:
        GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))
    except Exception:
        log.warning("ENV GROUP_CHAT_ID задан, но не int: %r", os.getenv("GROUP_CHAT_ID"))

# ——— Текст приветствия /start (исправленный)
START_TEXT: str = (
    "✅ Я уже тут!\n"
    "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n"
    "👉 Или нажмите команду /rent — я задам несколько вопросов о жилье, "
    "сформирую заявку, предложу варианты и передам менеджеру.\n"
    "Он свяжется с вами для уточнения деталей и бронирования."
)

# ——— Ключевые слова для «недвижимости» (перехватываем свободный чат)
REALTY_KEYWORDS = {
    "аренда","сдать","сниму","снять","дом","вилла","квартира","комнаты","спальни",
    "покупка","купить","продажа","продать","недвижимость","кондо","condo","таунхаус",
    "bungalow","bungalo","house","villa","apartment","rent","buy","sale","lease","property",
    "lamai","ламай","бопхут","маенам","чонг мон","чавенг","bophut","maenam","choeng mon","chaweng"
}

# ——— Фразы-конкуренты, которые GPT не должен советовать
BLOCK_PATTERNS = (
    "местных агентств","других агентств","на facebook","в группах facebook",
    "агрегаторах","marketplace","airbnb","booking","renthub","fazwaz",
    "dotproperty","list with","contact local agencies","facebook groups",
)

# ——— Состояния анкеты /rent
TYPE, BUDGET, AREA, BEDROOMS, NOTES = range(5)

# ——— Настройки webhook/Render
DEFAULT_PORT = 10000


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def env_required(name: str) -> str:
    """Достаёт переменную окружения и валидирует наличие."""
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"ENV {name} is required but missing")
    return val


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def mentions_realty(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in REALTY_KEYWORDS)


def sanitize_competitors(text: str) -> str:
    """Если GPT вдруг советует конкурентов — заменяем на наш CTA."""
    if not text:
        return text
    low = text.lower()
    if any(p in low for p in BLOCK_PATTERNS):
        msg, _ = build_cta_public()
        return "Чтобы не тратить время на сторонние площадки, лучше сразу к нам.\n\n" + msg
    return text


# ----------------------------------------------------------------------------
# 2) МОДЕЛЬ ЗАЯВКИ
# ----------------------------------------------------------------------------

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
    def from_context(cls, update: Update, form: Dict[str, str]) -> "Lead":
        return cls(
            created_at=now_str(),
            user_id=str(update.effective_user.id),
            username=update.effective_user.username or "",
            first_name=update.effective_user.first_name or "",
            type=form.get("type", ""),
            area=form.get("area", ""),
            budget=form.get("budget", ""),
            bedrooms=form.get("bedrooms", ""),
            notes=form.get("notes", ""),
        )

    def as_row(self) -> list[str]:
        return [
            self.created_at,
            self.user_id,
            self.username,
            self.first_name,
            self.type,
            self.area,
            self.budget,
            self.bedrooms,
            self.notes,
            self.source,
        ]


# ----------------------------------------------------------------------------
# 3) GOOGLE SHEETS CLIENT c ретраями
# ----------------------------------------------------------------------------

class SheetsClient:
    def __init__(self, sheet_id: str, sheet_name: str = "Leads"):
        self.sheet_id = sheet_id
        self.sheet_name = sheet_name
        self._gc = None
        self._ws = None

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
        # если лист пустой — добавим заголовки
        vals = self._ws.get_all_values()
        if not vals:
            self._ws.append_row(
                ["created_at","user_id","username","first_name","type","area","budget","bedrooms","notes","source"],
                value_input_option="USER_ENTERED"
            )

    def append_lead(self, lead: Lead, retries: int = 3, backoff: float = 1.0) -> Tuple[bool, Optional[str]]:
        """Возвращает (ok, row_url)."""
        self._open_ws()
        for attempt in range(1, retries + 1):
            try:
                self._ws.append_row(lead.as_row(), value_input_option="USER_ENTERED")
                row_url = f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/edit#gid={self._ws.id}"
                return True, row_url
            except Exception as e:
                log.warning("Sheets append failed (attempt %s/%s): %s", attempt, retries, e)
                time.sleep(backoff)
                backoff = min(backoff * 2, 8)
        return False, None


# ----------------------------------------------------------------------------
# 4) НОТИФИКАТОР
# ----------------------------------------------------------------------------

class Notifier:
    def __init__(self, manager_chat_id: Optional[int], group_chat_id: Optional[int]):
        self.manager_chat_id = manager_chat_id
        self.group_chat_id = group_chat_id

    async def notify_new_lead(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        lead: Lead,
        row_url: Optional[str],
    ) -> None:
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

        targets = [cid for cid in (self.manager_chat_id, self.group_chat_id) if cid]
        for chat_id in targets:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    disable_web_page_preview=True
                )
            except Exception as e:
                log.warning("Notify failed for %s: %s", chat_id, e)


# ----------------------------------------------------------------------------
# 5) CTA / кнопки / тексты
# ----------------------------------------------------------------------------

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
    if MANAGER_TG_URL:
        kb.inline_keyboard.append([InlineKeyboardButton("👤 Написать менеджеру", url=MANAGER_TG_URL)])
        msg += "\n\n👤 Контакт менеджера открыт ниже."
    return msg, kb


# ----------------------------------------------------------------------------
# 6) АНКЕТА /rent
# ----------------------------------------------------------------------------

class RentFlow:
    """Диалог подбора недвижимости с записью в Sheets и нотификацией."""

    def __init__(self, sheets: SheetsClient, notifier: Notifier):
        self.sheets = sheets
        self.notifier = notifier

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["form"] = {}
        await update.message.reply_text(
            "Начнём подбор.\n1/5. Какой тип жилья интересует: квартира, дом или вилла?"
        )
        return TYPE

    async def set_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["form"]["type"] = (update.message.text or "").strip()
        await update.message.reply_text("2/5. Какой у вас бюджет в батах (месяц)?")
        return BUDGET

    async def set_budget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["form"]["budget"] = (update.message.text or "").strip()
        await update.message.reply_text("3/5. В каком районе Самуи предпочтительно жить?")
        return AREA

    async def set_area(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["form"]["area"] = (update.message.text or "").strip()
        await update.message.reply_text("4/5. Сколько нужно спален?")
        return BEDROOMS

    async def set_bedrooms(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["form"]["bedrooms"] = (update.message.text or "").strip()
        await update.message.reply_text("5/5. Важные условия? (близость к пляжу, с питомцами, парковка и т.п.)")
        return NOTES

    async def finish(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["form"]["notes"] = (update.message.text or "").strip()
        form = context.user_data["form"]
        lead = Lead.from_context(update, form)

        ok, row_url = self.sheets.append_lead(lead)
        if not ok:
            log.error("Не удалось записать в Google Sheets")

        # помечаем, что анкета пройдена (разрешит контакт менеджера)
        context.user_data["rental_form_completed"] = True

        # шлём уведомления менеджеру/в группу
        await self.notifier.notify_new_lead(update, context, lead, row_url=row_url)

        # пользователю — ссылки + менеджер
        msg, kb = build_cta_with_manager()
        await update.message.reply_text(
            "Заявка сохранена ✅\n" + msg,
            reply_markup=kb,
            disable_web_page_preview=True
        )
        return ConversationHandler.END

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text("Окей, если передумаете — пишите /rent.")
        return ConversationHandler.END


# ----------------------------------------------------------------------------
# 7) СВОБОДНЫЙ ЧАТ
# ----------------------------------------------------------------------------

async def call_gpt(user_text: str) -> str:
    """
    Заглушка: при желании подключи свой LLM.
    Политика Cozy Asia: не советовать сторонние агентства/FB-группы/агрегаторы.
    """
    return "Готов помочь. По вопросам недвижимости лучше сразу у нас — жмите /rent или смотрите ссылки ниже."

async def free_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text or ""
    completed = bool(context.user_data.get("rental_form_completed", False))

    if mentions_realty(text):
        msg, kb = (build_cta_with_manager() if completed else build_cta_public())
        await update.effective_message.reply_text(
            msg, reply_markup=kb, disable_web_page_preview=True
        )
        return

    reply = sanitize_competitors(await call_gpt(text))
    await update.effective_message.reply_text(reply, disable_web_page_preview=True)


# ----------------------------------------------------------------------------
# 8) СЛУЖЕБНЫЕ КОМАНДЫ
# ----------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш Chat ID: {update.effective_chat.id}\nВаш User ID: {update.effective_user.id}"
    )

async def cmd_groupid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Group chat ID: {update.effective_chat.id}")


# ----------------------------------------------------------------------------
# 9) WEBHOOK (Render)
# ----------------------------------------------------------------------------

def preflight_release_webhook(token: str):
    """
    На всякий: удалим старый webhook/очередь, чтобы не было дублей.
    Для webhook это не критично, но безопасно.
    """
    base = f"https://api.telegram.org/bot{token}"
    try:
        requests.post(f"{base}/deleteWebhook", params={"drop_pending_updates": True}, timeout=10)
        log.info("deleteWebhook -> OK")
    except Exception as e:
        log.warning("deleteWebhook error: %s", e)


# ----------------------------------------------------------------------------
# 10) MAIN
# ----------------------------------------------------------------------------

def build_application() -> Tuple[Application, RentFlow]:
    token = env_required("TELEGRAM_BOT_TOKEN")

    # Google Sheets клиент
    sheet_id = env_required("GOOGLE_SHEETS_DB_ID")
    sheet_name = os.getenv("GOOGLE_SHEETS_SHEET_NAME", "Leads")
    sheets = SheetsClient(sheet_id=sheet_id, sheet_name=sheet_name)

    # Нотификатор
    notifier = Notifier(manager_chat_id=MANAGER_CHAT_ID, group_chat_id=GROUP_CHAT_ID)

    # RentFlow
    flow = RentFlow(sheets=sheets, notifier=notifier)

    # Telegram application
    app = ApplicationBuilder().token(token).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("groupid", cmd_groupid))

    # Анкета /rent
    conv = ConversationHandler(
        entry_points=[CommandHandler("rent", flow.start)],
        states={
            TYPE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, flow.set_type)],
            BUDGET:    [MessageHandler(filters.TEXT & ~filters.COMMAND, flow.set_budget)],
            AREA:      [MessageHandler(filters.TEXT & ~filters.COMMAND, flow.set_area)],
            BEDROOMS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, flow.set_bedrooms)],
            NOTES:     [MessageHandler(filters.TEXT & ~filters.COMMAND, flow.finish)],
        },
        fallbacks=[CommandHandler("cancel", flow.cancel)],
    )
    app.add_handler(conv)

    # Свободный чат — ставим после команд/диалога
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_handler))

    return app, flow


def main():
    token = env_required("TELEGRAM_BOT_TOKEN")
    base_url = os.getenv("WEBHOOK_BASE") or os.getenv("RENDER_EXTERNAL_URL")
    if not base_url:
        raise RuntimeError("WEBHOOK_BASE (или RENDER_EXTERNAL_URL) не задан. Укажи публичный https URL Render-сервиса.")

    preflight_release_webhook(token)

    app, _ = build_application()

    port = int(os.getenv("PORT", str(DEFAULT_PORT)))  # Render даёт $PORT
    url_path = token                                   # секретный путь = токен
    webhook_url = f"{base_url.rstrip('/')}/webhook/{url_path}"

    log.info("Starting webhook on 0.0.0.0:%s | url=%s", port, webhook_url)

    # Поднимаем сервер webhook (PTB сам поднимет aiohttp)
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
