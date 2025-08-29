# main.py — Cozy Asia Bot (ptb v20+)
# Полная версия: анкета /rent -> Sheets, редиректор на свои ресурсы,
# показ контакта менеджера только ПОСЛЕ анкеты, дублирование заявок в личку и в группу,
# preflight для polling-слота, команды /id и /groupid.

import os
import json
import time
import logging
from datetime import datetime

import requests
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

# ── ЛОГИ ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cozyasia-bot")

# ── КОНСТАНТЫ ССЫЛОК (твои данные) ──────────────────────────────────────────
WEBSITE_URL       = "https://www.cozy-asiath.com/"
TG_CHANNEL_MAIN   = "https://t.me/SamuiRental"
TG_CHANNEL_VILLAS = "https://t.me/arenda_vill_samui"
INSTAGRAM_URL     = "https://www.instagram.com/cozy.asia?igsh=cmt1MHA0ZmM3OTRu"

# Менеджер (контакт показываем ТОЛЬКО ПОСЛЕ анкеты):
MANAGER_TG_URL  = "https://t.me/cozy_asia"   # @Cozy_asia
MANAGER_CHAT_ID = 5978240436                 # Cozy Asia manager

# Рабочая группа (подставь свой -100… когда узнаешь):
GROUP_CHAT_ID = None

# ── ПРИВЕТСТВИЕ (исправленное) ──────────────────────────────────────────────
START_TEXT = (
    "✅ Я уже тут!\n"
    "🌴 Можете спросить меня о вашем пребывании на острове — подскажу и помогу.\n"
    "👉 Или нажмите команду /rent — я задам несколько вопросов о жилье, "
    "сформирую заявку, предложу варианты и передам менеджеру.\n"
    "Он свяжется с вами для уточнения деталей и бронирования."
)

# ── КНОПКИ / CTA ─────────────────────────────────────────────────────────────
def build_cta_public() -> tuple[str, InlineKeyboardMarkup]:
    kb = [
        [InlineKeyboardButton("🌐 Открыть сайт", url=WEBSITE_URL)],
        [InlineKeyboardButton("📣 Наш Telegram-канал (все лоты)", url=TG_CHANNEL_MAIN)],
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
    if MANAGER_TG_URL:
        kb.inline_keyboard.append([InlineKeyboardButton("👤 Написать менеджеру", url=MANAGER_TG_URL)])
        msg += "\n\n👤 Контакт менеджера открыт ниже."
    return msg, kb

# ── БЛОКИРАТОР УПОМИНАНИЙ КОНКУРЕНТОВ ───────────────────────────────────────
BLOCK_PATTERNS = (
    "местных агентств","других агентств","на facebook","в группах facebook",
    "агрегаторах","marketplace","airbnb","booking","renthub","fazwaz",
    "dotproperty","list with","contact local agencies","facebook groups"
)
def sanitize_competitors(text: str) -> str:
    if not text:
        return text
    low = text.lower()
    if any(p in low for p in BLOCK_PATTERNS):
        msg, _ = build_cta_public()
        return "Чтобы не тратить время на сторонние площадки, лучше сразу к нам.\n\n" + msg
    return text

# ── РЕАЛТИ-ТРИГГЕРЫ (ловим свободные вопросы) ───────────────────────────────
REALTY_KEYWORDS = {
    "аренда","сдать","сниму","снять","дом","вилла","квартира","комнаты","спальни",
    "покупка","купить","продажа","продать","недвижимость","кондо","condo","таунхаус",
    "bungalow","bungalo","house","villa","apartment","rent","buy","sale","lease","property",
    "lamai","ламай","бопхут","маенам","чонг мон","чавенг","bophut","maenam","choeng mon","chaweng"
}
def mentions_realty(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in REALTY_KEYWORDS)

# ── АНКЕТА /rent ─────────────────────────────────────────────────────────────
TYPE, BUDGET, AREA, BEDROOMS, NOTES = range(5)

async def rent_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["form"] = {}
    await update.message.reply_text(
        "Начнём подбор.\n1/5. Какой тип жилья интересует: квартира, дом или вилла?"
    )
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

    ok, row_url = await write_lead_to_sheets(update, context, form)
    context.user_data["rental_form_completed"] = True  # допускаем контакт менеджера
    await notify_staff(update, context, form, row_url=row_url)

    msg, kb = build_cta_with_manager()
    await update.message.reply_text("Заявка сохранена ✅\n" + msg,
                                    reply_markup=kb, disable_web_page_preview=True)
    return ConversationHandler.END

async def rent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Окей, если передумаете — пишите /rent.")
    return ConversationHandler.END

# ── GOOGLE SHEETS ────────────────────────────────────────────────────────────
# ENV:
# TELEGRAM_BOT_TOKEN
# GOOGLE_SERVICE_ACCOUNT_JSON  (полный JSON ключа)
# GOOGLE_SHEETS_DB_ID
# GOOGLE_SHEETS_SHEET_NAME (опц., по умолчанию 'Leads')
async def write_lead_to_sheets(update: Update, context: ContextTypes.DEFAULT_TYPE, form: dict):
    sheet_id = os.getenv("GOOGLE_SHEETS_DB_ID")
    if not sheet_id:
        log.warning("GOOGLE_SHEETS_DB_ID not set; skipping Sheets write.")
        return False, None

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        svc_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        info = json.loads(svc_json) if svc_json else {}
        creds = Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)
        ws_name = os.getenv("GOOGLE_SHEETS_SHEET_NAME", "Leads")
        try:
            ws = sh.worksheet(ws_name)
        except Exception:
            ws = sh.add_worksheet(title=ws_name, rows=1000, cols=20)

        # заголовки
        if not ws.get_all_values():
            ws.append_row(
                ["created_at","user_id","username","first_name","type","area","budget","bedrooms","notes","source"],
                value_input_option="USER_ENTERED"
            )

        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            str(update.effective_user.id),
            update.effective_user.username or "",
            update.effective_user.first_name or "",
            form.get("type",""),
            form.get("area",""),
            form.get("budget",""),
            form.get("bedrooms",""),
            form.get("notes",""),
            "telegram_bot",
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")

        # ссылка на лист (для удобства)
        row_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={ws.id}"
        return True, row_url
    except Exception as e:
        log.exception("Sheets write failed: %s", e)
        return False, None

# ── УВЕДОМЛЕНИЯ КОМАНДЕ ─────────────────────────────────────────────────────
async def notify_staff(update: Update, context: ContextTypes.DEFAULT_TYPE, form: dict, row_url: str | None):
    text = (
        "🆕 Новая заявка Cozy Asia\n\n"
        f"Клиент: @{update.effective_user.username or 'без_username'} "
        f"(ID: {update.effective_user.id})\n"
        f"Тип: {form.get('type','—')}\n"
        f"Район: {form.get('area','—')}\n"
        f"Бюджет: {form.get('budget','—')}\n"
        f"Спален: {form.get('bedrooms','—')}\n"
        f"Условия/прим.: {form.get('notes','—')}\n"
        f"Создано: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    if row_url:
        text += f"\n🗂 Таблица: {row_url}"

    targets = [cid for cid in (MANAGER_CHAT_ID, GROUP_CHAT_ID) if cid]
    for chat_id in targets:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
        except Exception as e:
            log.warning("Notify failed for %s: %s", chat_id, e)

# ── GPT-фолбэк (заглушка с политикой) ───────────────────────────────────────
async def call_gpt(user_text: str) -> str:
    return "Готов помочь. По вопросам недвижимости лучше сразу у нас — жмите /rent или смотрите ссылки ниже."

# ── СВОБОДНЫЙ ЧАТ (перехватывает realty-вопросы) ────────────────────────────
async def free_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text or ""
    completed = bool(context.user_data.get("rental_form_completed", False))

    if mentions_realty(text):
        msg, kb = (build_cta_with_manager() if completed else build_cta_public())
        await update.effective_message.reply_text(msg, reply_markup=kb, disable_web_page_preview=True)
        return

    reply = sanitize_competitors(await call_gpt(text))
    await update.effective_message.reply_text(reply, disable_web_page_preview=True)

# ── СЛУЖЕБНЫЕ КОМАНДЫ ───────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш Chat ID: {update.effective_chat.id}\nВаш User ID: {update.effective_user.id}")

async def cmd_groupid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Group chat ID: {update.effective_chat.id}")

# ── PREFLIGHT: жёстко освобождаем polling-слот ──────────────────────────────
def preflight_release_slot(token: str, attempts: int = 6):
    base = f"https://api.telegram.org/bot{token}"
    try:
        requests.post(f"{base}/deleteWebhook", params={"drop_pending_updates": True}, timeout=10)
        log.info("deleteWebhook -> OK")
    except Exception as e:
        log.warning("deleteWebhook error: %s", e)
    backoff = 2
    for i in range(1, attempts + 1):
        try:
            r = requests.post(f"{base}/close", timeout=10)
            if r.ok and r.json().get("ok"):
                log.info("close -> OK (attempt %s)", i)
                break
        except Exception as e:
            log.warning("close error: %s", e)
        time.sleep(backoff)
        backoff = min(backoff * 2, 20)

# ── MAIN ────────────────────────────────────────────────────────────────────
def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    preflight_release_slot(token)

    app = ApplicationBuilder().token(token).build()

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

    # Свободный чат (ставим ДО других текст-хэндлеров)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_handler))

    log.info("Bot is running…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
