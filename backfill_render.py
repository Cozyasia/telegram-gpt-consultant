# backfill_render.py
# ------------------------------------------------------------
# ENV (любое из имён подойдёт — берётся первое найденное):
#  API_ID | TELEGRAM_API_ID                (int)
#  API_HASH | TELEGRAM_API_HASH            (str)
#  SESSION_STRING | TELEGRAM_SESSION       (str, обязательно для user-сессии)
#  BOT_TOKEN | TELEGRAM_BOT_TOKEN          (str, используем только если разрешено)
#  ALLOW_BOT_BACKFILL                      ("1" для разрешения боту читать историю)
#  CHANNELS | CHANNEL_USERNAME | CHANNEL   (строка или список через запятую)
#  GOOGLE_SHEETS_DB_ID                     (ID таблицы)
#  GOOGLE_SERVICE_ACCOUNT_JSON             (целиком JSON одной строкой ИЛИ путь к .json)
#  LISTINGS_TAB                            (по умолчанию "Listings")
#  BACKFILL_LIMIT                          (сколько сообщений тянуть, по умолчанию 1000)
# ------------------------------------------------------------

import os
import re
import json
import asyncio
from datetime import datetime
from typing import Iterable, List, Tuple, Optional, Set

import gspread
from google.oauth2.service_account import Credentials

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

# ---------- helpers ----------

def env_any(*names: str, default: Optional[str] = None) -> Optional[str]:
    """Вернуть первое непустое значение из списка имён env."""
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return v
    return default

def to_int(v: Optional[str]) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except Exception:
        return None

def normalize_channels(raw: str) -> List[str]:
    """Принимает @name, https://t.me/name, t.me/c/123, -100123..., перечисленные через запятую."""
    out = []
    for piece in (raw or "").split(","):
        s = piece.strip()
        if not s:
            continue
        # URL -> короткое имя или id
        s = re.sub(r"^(https?://)?t\.me/(c/)?", "", s, flags=re.I)
        # t.me/c/123/45 -> -100123
        m = re.match(r"^c/(\d+)(?:/.*)?$", s, flags=re.I)
        if m:
            out.append(f"-100{m.group(1)}")
            continue
        # убрать ведущий @
        s = s[1:] if s.startswith("@") else s
        out.append(s)
    return out

def parse_price_bedrooms(text: str) -> Tuple[Optional[int], Optional[int]]:
    price = None
    m = re.search(r'(?:(?:฿|THB)\s*)?([0-9]{2,3}(?:[ \u00A0]?[0-9]{3})+|[0-9]{4,6})\b', text, re.I)
    if m:
        price = int(re.sub(r'\D', '', m.group(1)))
    br = None
    m2 = re.search(r'(\d+)\s*(?:спал|bed|beds|br)\b', text, re.I)
    if m2:
        br = int(m2.group(1))
    return price, br

# ---------- Google Sheets ----------

def load_gsa_credentials(val: str) -> Credentials:
    """Принимает либо JSON одной строкой, либо путь к файлу."""
    if not val:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON не задан.")
    try:
        info = json.loads(val)
    except json.JSONDecodeError:
        # Возможно, это путь к файлу
        with open(val, "r", encoding="utf-8") as f:
            info = json.load(f)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    return Credentials.from_service_account_info(info, scopes=scopes)

def open_listings_ws(sheet_id: str, tab: str) -> gspread.Worksheet:
    creds = load_gsa_credentials(env_any("GOOGLE_SERVICE_ACCOUNT_JSON"))
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(tab, rows=1000, cols=20)
        ws.update("A1:H1", [[
            "ts", "source", "tg_message_id", "channel", "title",
            "price", "bedrooms", "text"
        ]])
    return ws

def existing_ids(ws: gspread.Worksheet) -> Set[str]:
    try:
        col = ws.col_values(3)  # "tg_message_id"
        return {c.strip() for c in col[1:] if c}
    except Exception:
        return set()

# ---------- Telegram client ----------

async def get_client() -> TelegramClient:
    api_id  = to_int(env_any("TELEGRAM_API_ID", "API_ID"))
    api_hash = env_any("TELEGRAM_API_HASH", "API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("API_ID/API_HASH не заданы (ни TELEGRAM_*, ни без префикса).")

    session_str = env_any("TELEGRAM_SESSION", "SESSION_STRING")
    bot_token   = env_any("TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
    allow_bot   = env_any("ALLOW_BOT_BACKFILL") in ("1", "true", "yes", "on")

    # всегда предпочитаем user-сессию
    if session_str:
        client = TelegramClient(StringSession(session_str), api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("TELEGRAM_SESSION/SESSION_STRING не авторизована.")
        print(">> using USER session")
        return client

    if bot_token and allow_bot:
        client = TelegramClient("bot", api_id, api_hash)
        await client.start(bot_token=bot_token)
        print(">> using BOT token (ALLOW_BOT_BACKFILL=1)")
        return client

    raise RuntimeError(
        "Нет user-сессии. Задай TELEGRAM_SESSION (или SESSION_STRING).\n"
        "Бот-токен не подойдёт для чтения истории (если только ALLOW_BOT_BACKFILL=1 и у бота есть права)."
    )

# ---------- core backfill ----------

async def pull_channel(client: TelegramClient, entity: str, ws: gspread.Worksheet,
                       known_ids: Set[str], limit: int) -> int:
    """
    Возвращает количество добавленных строк по одному каналу.
    """
    added = 0
    buf: List[List[object]] = []

    async for msg in client.iter_messages(entity, limit=limit):
        if not msg or not msg.id:
            continue

        msg_id = str(msg.id)
        if msg_id in known_ids:
            continue

        text = (msg.message or "").strip()
        if not text and getattr(msg, "caption", None):
            text = (msg.caption or "").strip()

        price, br = parse_price_bedrooms(text or "")
        title = (text.splitlines()[0][:120] if text else "")

        ts = (msg.date or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
        row = [
            ts, "channel", msg_id, str(entity), title,
            price if price is not None else "", br if br is not None else "", text or ""
        ]
        buf.append(row)
        known_ids.add(msg_id)

        if len(buf) >= 100:
            ws.append_rows(buf, value_input_option="RAW")
            added += len(buf)
            print(f"... saved {len(buf)} rows from {entity}")
            buf.clear()

    if buf:
        ws.append_rows(buf, value_input_option="RAW")
        added += len(buf)
        print(f"... saved {len(buf)} rows from {entity}")

    return added

async def backfill():
    channels_raw = env_any("CHANNELS", "CHANNEL_USERNAME", "CHANNEL")
    if not channels_raw:
        raise RuntimeError("Не указаны CHANNELS/CHANNEL_USERNAME/CHANNEL.")
    channels = normalize_channels(channels_raw)

    sheet_id = env_any("GOOGLE_SHEETS_DB_ID")
    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEETS_DB_ID не задан.")

    tab = env_any("LISTINGS_TAB", default="Listings")
    limit = to_int(env_any("BACKFILL_LIMIT", default="1000")) or 1000

    print("==> Backfill started")
    print(f"Channels: {channels}")
    print(f"Sheet: {sheet_id}, tab: {tab}, limit: {limit}")

    ws = open_listings_ws(sheet_id, tab)
    known = existing_ids(ws)

    client = await get_client()

    total_added = 0
    try:
        for ch in channels:
            try:
                print(f"-> pulling {ch}")
                added = await pull_channel(client, ch, ws, known, limit)
                total_added += added
            except errors.FloodWaitError as e:
                delay = int(getattr(e, "seconds", 30)) + 1
                print(f"FloodWait {delay}s on {ch}, sleeping...")
                await asyncio.sleep(delay)
            except errors.BotMethodInvalidError:
                raise RuntimeError(
                    "BotMethodInvalidError: бот не может читать историю. "
                    "Используй user-сессию (SESSION_STRING/TELEGRAM_SESSION) "
                    "или поставь ALLOW_BOT_BACKFILL=1 и дай боту нужные права в канале."
                )
            except Exception as e:
                print(f"!! error on {ch}: {e}")
    finally:
        await client.disconnect()

    print(f"==> Backfill finished, added {total_added} rows")

# ---------- entry ----------

if __name__ == "__main__":
    asyncio.run(backfill())
