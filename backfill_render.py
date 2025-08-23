# backfill_render.py
import os
import re
import json
import base64
import asyncio
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import rpcerrorlist


# ---------- utils ----------
def env_any(*keys, default=None, cast=str):
    """Вернуть первое непустое значение из списка ключей ENV."""
    for k in keys:
        v = os.environ.get(k)
        if v is not None and str(v).strip() != "":
            return cast(v) if cast and v is not None else v
    return default


def sanitize_session_str(s: str) -> str:
    """
    Оставить только base64url-символы и убрать 'SESSION_STRING=' если вдруг вставили.
    Это спасает от неразрывных пробелов/«красивых» кавычек при копипасте с телефона.
    """
    s = (s or "").strip()
    if s.lower().startswith("session_string="):
        s = s.split("=", 1)[1].strip()
    # Разрешены только A-Za-z0-9 - _ =
    s = "".join(ch for ch in s if (ch.isalnum() or ch in "-_="))
    return s


def load_gsa_info(raw: str) -> dict:
    """
    Принять JSON сервис-аккаунта тремя способами:
    1) обычный JSON-текст
    2) base64(JSON)
    3) путь к файлу .json
    """
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is empty")

    raw = raw.strip()

    # 1) Пытаемся как обычный JSON
    try:
        return json.loads(raw)
    except Exception:
        pass

    # 2) Пытаемся как base64(JSON)
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        pass

    # 3) Пытаемся как путь к файлу
    try:
        with open(raw, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        pass

    raise RuntimeError(
        "GOOGLE_SERVICE_ACCOUNT_JSON: передай валидный JSON, "
        "base64(JSON) или путь к .json"
    )


def normalize_channel(s: str):
    """Поддержка @username, username, t.me/..., числовых id."""
    s = (s or "").strip()
    if not s:
        return s
    # t.me/... или t.me/c/...
    m = re.search(r"t\.me/(?:c/)?([^/?#]+)", s, re.IGNORECASE)
    if m:
        s = m.group(1)
    # без @ тоже норм, Telethon примет оба варианта
    return s


def parse_price_bedrooms(text: str):
    price = None
    m = re.search(
        r'(?:(?:฿|THB)\s*)?([0-9]{2,3}(?:[ \u00A0]?[0-9]{3})+|[0-9]{4,6})\b',
        text,
        re.I,
    )
    if m:
        price = int(re.sub(r"\D", "", m.group(1)))

    br = None
    m2 = re.search(r"(\d+)\s*(?:спал|bed|br)", text, re.I)
    if m2:
        br = int(m2.group(1))

    return price, br


def open_listings_ws(sheet_id: str, tab: str, gsa_raw: str):
    info = load_gsa_info(gsa_raw)
    creds = Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(tab, rows=1000, cols=20)
        ws.update(
            "A1:H1",
            [
                [
                    "ts",
                    "source",
                    "tg_message_id",
                    "channel",
                    "title",
                    "price",
                    "bedrooms",
                    "text",
                ]
            ],
        )
    return ws


async def build_client(api_id: int, api_hash: str):
    """Выбрать USER (SESSION_STRING) или BOT (BOT_TOKEN)."""
    bot_token = env_any("TELEGRAM_BOT_TOKEN")
    session_str = env_any("TELEGRAM_SESSION", "SESSION_STRING")

    if session_str:
        ss = sanitize_session_str(session_str)
        if not ss:
            raise RuntimeError(
                "Строка сессии пустая после очистки. Вставь ровно значение без 'SESSION_STRING=' и без кавычек."
            )
        print(">> using USER session")
        client = TelegramClient(StringSession(ss), api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "USER session не авторизована. Сгенерируй новую SESSION_STRING."
            )
        return client

    if bot_token:
        print(">> using BOT token")
        client = TelegramClient("bot", api_id, api_hash)
        await client.start(bot_token=bot_token)
        return client

    raise RuntimeError(
        "Не найдены TELEGRAM_SESSION/SESSION_STRING и TELEGRAM_BOT_TOKEN. "
        "Интерактив на Render запрещён."
    )


# ---------- main ----------
async def backfill():
    api_id = env_any("TELEGRAM_API_ID", "API_ID", cast=int)
    api_hash = env_any("TELEGRAM_API_HASH", "API_HASH")
    channel_cfg = env_any("CHANNEL_USERNAME", "CHANNEL")
    sheet_id = env_any("GOOGLE_SHEETS_DB_ID", "SHEET_ID")
    tab = env_any("LISTINGS_TAB", default="Listings")
    gsa_json = env_any("GOOGLE_SERVICE_ACCOUNT_JSON")
    limit = env_any("BACKFILL_LIMIT", default="1000")
    try:
        limit = int(limit)
    except Exception:
        limit = 1000

    if not api_id or not api_hash:
        raise RuntimeError("Не заданы TELEGRAM_API_ID/API_ID и TELEGRAM_API_HASH/API_HASH.")
    if not channel_cfg:
        raise RuntimeError("Не задан CHANNEL_USERNAME/CHANNEL.")
    if not sheet_id:
        raise RuntimeError("Не задан GOOGLE_SHEETS_DB_ID/SHEET_ID.")
    if not gsa_json:
        raise RuntimeError("Не задан GOOGLE_SERVICE_ACCOUNT_JSON.")

    channel = normalize_channel(channel_cfg)
    ws = open_listings_ws(sheet_id, tab, gsa_json)

    # собираем уже сохранённые msg_id
    existing_ids = set()
    try:
        col = ws.col_values(3)  # tg_message_id
        for v in col[1:]:
            if v:
                existing_ids.add(v.strip())
    except Exception:
        pass

    print(f"==> Backfill started: channel={channel}, limit={limit}, known_ids={len(existing_ids)}")

    client = await build_client(api_id, api_hash)

    new_rows = []
    saved_total = 0
    try:
        async for msg in client.iter_messages(channel, limit=limit):
            if not msg or not msg.id:
                continue
            mid = str(msg.id)
            if mid in existing_ids:
                continue

            text = (msg.message or "").strip() or (msg.caption or "").strip()
            price, br = parse_price_bedrooms(text or "")
            title = (text.splitlines()[0][:120] if text else "")

            ts = (msg.date or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
            new_rows.append(
                [
                    ts,
                    "channel",
                    mid,
                    str(channel_cfg),
                    title,
                    price if price is not None else "",
                    br if br is not None else "",
                    text,
                ]
            )

            if len(new_rows) >= 100:
                ws.append_rows(new_rows, value_input_option="RAW")
                saved_total += len(new_rows)
                print(f"...saved {saved_total} rows")
                new_rows.clear()

        if new_rows:
            ws.append_rows(new_rows, value_input_option="RAW")
            saved_total += len(new_rows)
            print(f"...saved {saved_total} rows (final batch)")
    except rpcerrorlist.BotMethodInvalidError:
        print(
            "❗ BotMethodInvalidError: Telegram запрещает боту читать историю этого канала.\n"
            "Используй USER-сессию: установи TELEGRAM_SESSION/SESSION_STRING (строка сессии Telethon)."
        )
        raise
    finally:
        await client.disconnect()

    print("==> Backfill finished")


if __name__ == "__main__":
    asyncio.run(backfill())
