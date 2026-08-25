# -*- coding: utf-8 -*-
from __future__ import annotations
import asyncio, base64, hashlib, io, json, logging, os
from datetime import datetime, timezone
from telegram.ext import CommandHandler

log = logging.getLogger("mtproto-auth-big")
API_ID = os.environ.get("MT_API_ID", "").strip()
API_HASH = os.environ.get("MT_API_HASH", "").strip()
SESSION_KEY = os.environ.get("MT_SESSION_KEY", "").strip()
ADMIN = os.environ.get("MT_ADMIN_USERNAME", "Cozy_asia").strip().lstrip("@").lower()


def configured(): return bool(API_ID and API_HASH and SESSION_KEY)

def admin_ok(update):
    u, c = getattr(update, "effective_user", None), getattr(update, "effective_chat", None)
    return bool(u and c and getattr(c, "type", "") == "private" and (getattr(u, "username", "") or "").lower() == ADMIN)

def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(SESSION_KEY.encode()).digest()))

def _ws(catalog):
    sh = catalog._client().open_by_key(catalog.SHEET_ID)
    try: return sh.worksheet("MTProtoAuth")
    except Exception:
        ws = sh.add_worksheet(title="MTProtoAuth", rows=20, cols=5)
        ws.append_row(["key","value","updated_at","account_id","username"], value_input_option="RAW")
        return ws

def save_session(catalog, session, me):
    enc = _fernet().encrypt(session.encode()).decode()
    ws = _ws(catalog); vals = ws.get_all_values(); row_no = None
    for i, row in enumerate(vals[1:], start=2):
        if row and row[0] == "session": row_no = i; break
    payload = [["session", enc, datetime.now(timezone.utc).isoformat(timespec="seconds"), str(getattr(me,"id","") or ""), str(getattr(me,"username","") or "")]]
    if row_no: ws.update(f"A{row_no}:E{row_no}", payload, value_input_option="RAW")
    else: ws.append_row(payload[0], value_input_option="RAW")

def load_session(catalog):
    if not configured(): return ""
    try:
        for row in _ws(catalog).get_all_values()[1:]:
            if row and row[0] == "session" and len(row) > 1 and row[1]:
                return _fernet().decrypt(row[1].encode()).decode()
    except Exception: log.exception("Could not load MTProto session")
    return ""

async def new_client(catalog):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    session = await asyncio.to_thread(load_session, catalog)
    if not session: return None
    client = TelegramClient(StringSession(session), int(API_ID), API_HASH)
    await client.connect()
    if not await client.is_user_authorized(): await client.disconnect(); return None
    return client

async def status(update, context, catalog):
    if not admin_ok(update): return
    client = await new_client(catalog)
    if not client:
        await update.effective_message.reply_text("MTProto ещё не авторизован. Используйте /mtproto_qr")
        return
    try:
        me = await client.get_me(); await update.effective_message.reply_text(f"✅ MTProto авторизован: @{getattr(me,'username','') or 'без username'}")
    finally: await client.disconnect()

async def qr_login(update, context, catalog, on_success=None):
    if not admin_ok(update): return
    if not configured():
        await update.effective_message.reply_text("MTProto API ещё не настроен."); return
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import SessionPasswordNeededError, PasswordHashInvalidError
    import qrcode
    client = TelegramClient(StringSession(), int(API_ID), API_HASH); await client.connect(); qr = await client.qr_login()
    buf = io.BytesIO(); qrcode.make(qr.url).save(buf, format="PNG"); buf.seek(0)
    await update.effective_message.reply_photo(photo=buf, caption="Отсканируйте QR Premium-аккаунтом Cozy Asia: Telegram → Настройки → Устройства → Подключить устройство. Пароль 2FA в чат не присылайте.")
    async def finish():
        try:
            try: await qr.wait(timeout=120)
            except SessionPasswordNeededError:
                password = os.environ.get("MT_2FA_PASSWORD", "")
                if not password:
                    await update.effective_message.reply_text("QR подтверждён, но нужен пароль 2FA. Добавьте MT_2FA_PASSWORD прямо в Render этого сервиса, дождитесь деплоя и повторите /mtproto_qr."); return
                try: await client.sign_in(password=password)
                except PasswordHashInvalidError:
                    await update.effective_message.reply_text("MT_2FA_PASSWORD неверный. Исправьте значение в Render."); return
            me = await client.get_me(); await asyncio.to_thread(save_session, catalog, client.session.save(), me)
            await update.effective_message.reply_text(f"✅ MTProto-сессия сохранена для @{getattr(me,'username','') or 'аккаунта'}. Можно удалить MT_2FA_PASSWORD и запустить /premium_test")
            if on_success: on_success(catalog)
        except asyncio.TimeoutError: await update.effective_message.reply_text("QR истёк. Повторите /mtproto_qr")
        except Exception as e:
            log.exception("MTProto QR failed"); await update.effective_message.reply_text(f"MTProto-вход не завершён: {type(e).__name__}")
        finally:
            try: await client.disconnect()
            except Exception: pass
    context.application.create_task(finish())

def install(app, catalog, on_success=None):
    app.add_handler(CommandHandler("mtproto_status", lambda u,c: status(u,c,catalog)), group=-90)
    app.add_handler(CommandHandler("mtproto_qr", lambda u,c: qr_login(u,c,catalog,on_success)), group=-90)
