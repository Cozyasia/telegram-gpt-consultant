# main.py — Cozy Asia Bot (python-telegram-bot v21.6, webhook/Render)
# -----------------------------------------------------------------------------
# • /start — приветствие
# • /rent — анкета (7 шагов): type → budget → area → bedrooms → check-in → check-out → notes
#   - даты распознаются в любом привычном формате (сохраняем YYYY-MM-DD)
#   - запись в Google Sheets (если настроено), подборки-ссылки из Telegram-каналов
#   - уведомления менеджеру и в рабочую группу, кнопка “Написать менеджеру”
#   - анти-дубликаты: в течение 15 минут одинаковая заявка не шлётся повторно
# • Свободный GPT-чат (OpenAI) с фолбэками:
#   - если OpenAI недоступен, отвечаем кратко из локального FAQ по погоде/ветрам Самуи
#   - при “недвижимостных” темах — добавляем CTA Cozy Asia (без рекомендаций сторонних агентств)
# • /id, /groupid, /diag — сервис
#
# ENV:
#   TELEGRAM_BOT_TOKEN  (обяз.)
#   OPENAI_API_KEY      (обяз. для GPT)
#   WEBHOOK_BASE или RENDER_EXTERNAL_URL (обяз. публичный URL)
#   GOOGLE_SHEETS_DB_ID             (опц.)
#   GOOGLE_SERVICE_ACCOUNT_JSON     (опц., в одну строку)
#   GOOGLE_SHEETS_SHEET_NAME=Leads  (опц.)
#   GROUP_CHAT_ID                   (опц., ID рабочей группы)
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

# ───────────────────── ЛОГИ
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("cozyasia-bot")

# ───────────────────── ССЫЛКИ/КОНСТАНТЫ
WEBSITE_URL       = "https://cozy.asia"
TG_CHANNEL_MAIN   = "https://t.me/SamuiRental"
TG_CHANNEL_VILLAS = "https://t.me/arenda_vill_samui"
INSTAGRAM_URL     = "https://www.instagram.com/cozy.asia?igsh=cmt1MHA0ZmM3OTRu"
MAIN_CH_USERNAME   = "SamuiRental"
VILLAS_CH_USERNAME = "arenda_vill_samui"

MANAGER_TG_URL  = "https://t.me/Cozy_asia"   # показываем только после анкеты
MANAGER_CHAT_ID = 5978240436

GROUP_CHAT_ID: Optional[int] = None
if os.getenv("GROUP_CHAT_ID"):
    try:
        GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))
    except Exception:
        log.warning("GROUP_CHAT_ID из ENV не int: %r", os.getenv("GROUP_CHAT_ID"))

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
DUPLICATE_COOLDOWN_SEC = 15 * 60

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")

def env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"ENV {name} is required but missing")
    return v

def mentions_realty(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in REALTY_KEYWORDS)

def looks_like_realty_question(text: str) -> bool:
    return mentions_realty(text or "")

def sanitize_competitors(text: str) -> str:
    if not text: return text
    low = text.lower()
    if any(p in low for p in BLOCK_PATTERNS):
        msg, _ = build_cta_public()
        return "Чтобы не тратить время на сторонние площадки, лучше сразу к нам.\n\n" + msg
    return text

def parse_to_iso_date(text: str) -> str:
    s = (text or "").strip()
    if not s: return s
    try:
        dt = dtparser.parse(s, dayfirst=True,  yearfirst=False, fuzzy=True)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        try:
            dt = dtparser.parse(s, dayfirst=False, yearfirst=True, fuzzy=True)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return s

# ───────────────────── CTA/Кнопки
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

# ───────────────────── Поиск по каналам (быстрые ссылки)
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

# ───────────────────── Модель заявки
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

# ───────────────────── Google Sheets (опц.)
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

# ───────────────────── Уведомления
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

# ───────────────────── OpenAI + фолбэки
from openai import OpenAI, APIConnectionError, RateLimitError, APIStatusError

SYSTEM_PROMPT = (
    "Ты дружелюбный местный помощник Cozy Asia. Отвечай по делу и кратко.\n"
    "Можно свободно говорить о погоде, ветрах, районах, инфраструктуре, быте, школах и т.д.\n"
    "Если вопрос относится к аренде/покупке/продаже или «где посмотреть лоты», "
    "добавь в КОНЦЕ мягкий CTA: /rent + ссылки Cozy Asia. Никогда не советуй другие агентства или агрегаторы."
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

def local_weather_reply(user_text: str) -> Optional[str]:
    t = (user_text or "").lower()
    if any(w in t for w in ("погода","ветер","дожд","тайфун","шторм","волн")):
        # очень краткая памятка по Самуи
        month_map = {
            1:"январь: чаще солнечно, возможны краткие дожди; ветер NE/E, море спокойнее на западе.",
            2:"февраль: сухо и солнечно, слабый ветер NE/E; купаться комфортно почти везде.",
            3:"март: жарко, море спокойное; лёгкие бризы.",
            4:"апрель: самая жара; штиль, редкие ливни.",
            5:"май: жарко, начинают идти дожди, но короткие.",
            6:"июнь: переменная облачность, дожди краткие; море в целом спокойное.",
            7:"июль: комфортно, иногда волна на востоке/севере.",
            8:"август: похож на июль, умеренные ветра.",
            9:"сентябрь: чаще волна на востоке, заходить лучше на западе.",
            10:"октябрь: старт северо-восточного муссона; волны/дожди чаще на восточных пляжах.",
            11:"ноябрь: пик дождей; ветер NE/E, западные пляжи спокойнее.",
            12:"декабрь: дождливо, но улучшается к концу месяца; волна на востоке, уютнее на западе/юге."
        }
        for i, nm in enumerate(("январ","феврал","март","апрел","май","июн","июл","август","сентябр","октябр","ноябр","декабр"), start=1):
            if nm in t:
                return f"На Самуи {month_map[i]}\n⚠️ Погода тропическая и меняется быстро; для точного прогноза смотри за 1–3 дня до даты."
        return ("Самуи: климат тропический. Окт–дек — больше дождей и волна на востоке; "
                "янв–март — суше и спокойнее; апрель жаркий штиль; лето умеренное. "
                "За укрытием от волн часто лучше запад/юг.")
    return None

async def call_gpt(user_text: str) -> Optional[str]:
    """
    Возвращает None только при реальном сбое сети/ключа.
    Иначе — нормальный текст ответа (с пост-фильтром от конкурентов).
    """
    try:
        client = _get_openai()
        # 1) Responses API
        try:
            resp = client.responses.create(
                model="gpt-4o-mini",
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": (user_text or "").strip()},
                ],
                max_output_tokens=450,
                timeout=45,
            )
            txt = (resp.output_text or "").strip()
            if txt:
                return sanitize_competitors(txt)
        except Exception as e:
            log.warning("OpenAI responses.create failed: %s", e)

        # 2) Chat Completions fallback
        try:
            resp2 = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": (user_text or "").strip()},
                ],
                max_tokens=450,
                timeout=45,
            )
            txt2 = (resp2.choices[0].message.content or "").strip()
            if txt2:
                return sanitize_competitors(txt2)
        except Exception as e2:
            log.warning("OpenAI chat.completions.create failed: %s", e2)

        # 3) Локальный ответ (чтобы пользователь не видел «Упс…»)
        loc = local_weather_reply(user_text)
        if loc:
            return loc

        return "Могу помочь с этим. Расскажите, пожалуйста, что именно интересует — отвечу подробно."
    except (RateLimitError, APIStatusError, APIConnectionError) as e:
        log.error("OpenAI API error: %s", e)
        return local_weather_reply(user_text) or None
    except Exception as e:
        log.exception("OpenAI unexpected error")
        return local_weather_reply(user_text) or None

# ───────────────────── Анкета /rent
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
    context.user_data["form"]["checkin"] = parse_to_iso_date((update.message.text or "").strip())
    await update.message.reply_text("6/7. Дата выезда?")
    return CHECKOUT

async def rent_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["checkout"] = parse_to_iso_date((update.message.text or "").strip())
    await update.message.reply_text("7/7. Важные условия? (близость к пляжу, с питомцами, парковка и т.п.)")
    return NOTES

async def rent_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"]["notes"] = (update.message.text or "").strip()
    form = context.user_data["form"]
    lead = Lead.from_context(update, form)

    proposed_pairs = build_channel_search_links(form.get("area",""), form.get("bedrooms",""), form.get("budget",""))
    proposed_text_for_sheet = format_links_md(proposed_pairs)
    proposed_count = len(proposed_pairs)

    # анти-дубликат
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

    context.user_data["rental_form_completed"] = True

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

# ───────────────────── Свободный чат
async def free_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.effective_message.text or ""
    completed = bool(context.user_data.get("rental_form_completed", False))

    gpt_reply = await call_gpt(user_text)
    if gpt_reply is None:
        # полный сетевой сбой — даём мягкий ответ и CTA, чтобы диалог не “падал”
        base = local_weather_reply(user_text) or "Я на связи. Готов помочь с любыми вопросами!"
        if looks_like_realty_question(user_text):
            cta_msg, cta_kb = (build_cta_with_manager() if completed else build_cta_public())
            tail = (
                "\n\n🔧 Самый действенный способ — пройти короткую анкету командой /rent. "
                f"{'Менеджер уже в курсе и свяжется с вами.' if completed else 'Менеджер получит вашу заявку и свяжется.'}"
            )
            await update.effective_message.reply_text(base + tail + "\n\n" + cta_msg,
                                                      reply_markup=cta_kb, disable_web_page_preview=True)
        else:
            await update.effective_message.reply_text(base, disable_web_page_preview=True)
        return

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
    else:
        await update.effective_message.reply_text(gpt_reply, disable_web_page_preview=True)

# ───────────────────── Служебные команды
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш Chat ID: {update.effective_chat.id}\nВаш User ID: {update.effective_user.id}"
    )

async def cmd_groupid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Group chat ID: {update.effective_chat.id}")

async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Мини-диагностика OpenAI (делаем безопасно)
    openai_status = "NOT SET"
    err = None
    try:
        if os.getenv("OPENAI_API_KEY"):
            client = _get_openai()
            try:
                r = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role":"system","content":"Say OK"}],
                    max_tokens=3,
                    timeout=15,
                )
                openai_status = "OK" if (r.choices and r.choices[0].message.content) else "EMPTY"
            except Exception as e:
                err = str(e)
                openai_status = "ERROR"
    except Exception as e:
        err = str(e)
        openai_status = "ERROR(init)"

    txt = (
        "🔎 Диагностика:\n"
        f"• OPENAI: {openai_status}{(' — ' + err) if err else ''}\n"
        f"• GOOGLE_SHEETS_DB_ID: {os.getenv('GOOGLE_SHEETS_DB_ID') or '—'}\n"
        f"• GROUP_CHAT_ID: {os.getenv('GROUP_CHAT_ID') or str(GROUP_CHAT_ID or '—')}\n"
        f"• WEBHOOK_BASE: {os.getenv('WEBHOOK_BASE') or os.getenv('RENDER_EXTERNAL_URL') or '—'}"
    )
    await update.message.reply_text(txt)

# ───────────────────── Webhook utils
def preflight_release_webhook(token: str):
    base = f"https://api.telegram.org/bot{token}"
    try:
        requests.post(f"{base}/deleteWebhook", params={"drop_pending_updates": True}, timeout=10)
        log.info("deleteWebhook -> OK")
    except Exception as e:
        log.warning("deleteWebhook error: %s", e)

# ───────────────────── Bootstrap
def build_application() -> Application:
    token = env_required("TELEGRAM_BOT_TOKEN")

    # Sheets
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

    # Анкета
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

    # Свободный чат — обязательно последним
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
    url_path = token  # токен используем как часть пути
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
