# backfill_render.py
import os
import re
import asyncio
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from telethon import TelegramClient
from telethon.sessions import StringSession  # не используется в бот-режиме, но пусть будет

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SESSION_STR = os.environ.get("TELEGRAM_SESSION")  # на всякий случай поддерживаем и этот путь

CHANNEL = os.environ.get("CHANNEL_USERNAME") or os.environ.get("CHANNEL")
SHEET_ID = os.environ["GOOGLE_SHEETS_DB_ID"]
TAB = os.environ.get("LISTINGS_TAB", "Listings")
GSA_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

def parse_price_bedrooms(text: str):
    price = None
    m = re.search(r'(?:(?:฿|THB)\s*)?([0-9]{2,3}(?:[ \u00A0]?[0-9]{3})+|[0-9]{4,6})\b', text, re.I)
    if m:
        price = int(re.sub(r'\D', '', m.group(1)))
    br = None
    m2 = re.search(r'(\d+)\s*(?:спал|bed|br)', text, re.I)
    if m2:
        br = int(m2.group(1))
    return price, br

def open_listings_ws():
    import json
    info = json.loads(GSA_JSON)
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(TAB, rows=1000, cols=20)
        ws.update("A1:H1", [[
            "ts", "source", "tg_message_id", "channel", "title",
            "price", "bedrooms", "text"
        ]])
    return ws

async def get_client():
    if BOT_TOKEN:
        client = TelegramClient("bot", API_ID, API_HASH)
        await client.start(bot_token=BOT_TOKEN)
        return client
    elif SESSION_STR:
        client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("TELEGRAM_SESSION не авторизована.")
        return client
    else:
        raise RuntimeError("Задай TELEGRAM_BOT_TOKEN (или TELEGRAM_SESSION). Интерактив запрещён.")

async def backfill():
    print("==> Backfill started")
    if not CHANNEL:
        raise RuntimeError("Не указаны CHANNEL_USERNAME/CHANNEL.")
    ws = open_listings_ws()

    existing_ids = set()
    try:
        for v in ws.col_values(3)[1:]:
            if v:
                existing_ids.add(v.strip())
    except Exception:
        pass

    client = await get_client()
    entity = CHANNEL[1:] if CHANNEL.startswith("@") else CHANNEL

    new_rows = []
    async for msg in client.iter_messages(entity, limit=1000):
        if not msg or not msg.id:
            continue
        msg_id = str(msg.id)
        if msg_id in existing_ids:
            continue

        text = (msg.message or "").strip() or (msg.caption or "").strip()
        price, br = parse_price_bedrooms(text or "")
        title = (text.splitlines()[0][:120] if text else "")

        ts = (msg.date or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
        new_rows.append([
            ts, "channel", msg_id, str(CHANNEL), title,
            price if price is not None else "", br if br is not None else "", text
        ])

        if len(new_rows) >= 100:
            ws.append_rows(new_rows, value_input_option="RAW")
            print("...saved 100 rows")
            new_rows.clear()

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")

    await client.disconnect()
    print("==> Backfill finished")

if __name__ == "__main__":
    asyncio.run(backfill())
