# backfill_render.py
import os, re, json, base64, asyncio
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import rpcerrorlist

# ---------- helpers ----------
def env_any(*keys, default=None, cast=str):
    for k in keys:
        v = os.environ.get(k)
        if v is not None and str(v).strip() != "":
            return cast(v) if cast and v is not None else v
    return default

def sanitize_session_str(s: str) -> str:
    s = (s or "").strip()
    if s.lower().startswith("session_string="):
        s = s.split("=", 1)[1].strip()
    return "".join(ch for ch in s if ch.isalnum() or ch in "-_=")

def _strip_outer_quotes(s: str) -> str:
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1].strip()
    return s

def _try_json(raw: str):
    val = json.loads(raw)
    # иногда в ENV кладут JSON как строку в кавычках -> распарсим ещё раз
    if isinstance(val, str) and (val.strip().startswith("{") or val.strip().startswith("[")):
        return json.loads(val)
    if not isinstance(val, dict):
        raise ValueError("Expected dict for service account JSON")
    return val

def _try_b64_to_json(raw: str):
    s = _strip_outer_quotes(raw)
    # удалить все не base64url символы (включая неразрывные пробелы)
    s = re.sub(r"[^A-Za-z0-9_\-+/=]", "", s)
    # нормализуем к urlsafe: '+' -> '-', '/' -> '_'
    s = s.replace("+", "-").replace("/", "_")
    # добавим паддинг
    s += "=" * (-len(s) % 4)
    data = base64.urlsafe_b64decode(s.encode("ascii"))
    return _try_json(data.decode("utf-8"))

def load_gsa_info(raw: str) -> dict:
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is empty")
    raw = _strip_outer_quotes(raw)

    # 1) пробуем как чистый JSON
    try:
        info = _try_json(raw)
        print("GSA mode: JSON")
        return info
    except Exception:
        pass

    # 2) пробуем как base64(JSON)
    try:
        info = _try_b64_to_json(raw)
        print("GSA mode: BASE64")
        return info
    except Exception:
        pass

    # 3) пробуем как путь к файлу
    try:
        with open(raw, "r", encoding="utf-8") as f:
            info = json.load(f)
            print("GSA mode: FILE")
            return info
    except Exception:
        pass

    raise RuntimeError(
        "GOOGLE_SERVICE_ACCOUNT_JSON: передай валидный JSON, base64(JSON) или путь к .json"
    )

def normalize_channel(s: str):
    s = (s or "").strip()
    if not s:
        return s
    m = re.search(r"t\.me/(?:c/)?([^/?#]+)", s, re.I)
    if m:
        s = m.group(1)
    return s

def parse_price_bedrooms(text: str):
    price = None
    m = re.search(r'(?:(?:฿|THB)\s*)?([0-9]{2,3}(?:[ \u00A0]?[0-9]{3})+|[0-9]{4,6})\b', text, re.I)
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
        ws.update("A1:H1", [[
            "ts","source","tg_message_id","channel","title","price","bedrooms","text"
        ]])
    return ws

async def build_client(api_id: int, api_hash: str):
    bot_token = env_any("TELEGRAM_BOT_TOKEN")
    session_str = env_any("TELEGRAM_SESSION","SESSION_STRING")
    if session_str:
        ss = sanitize_session_str(session_str)
        if not ss:
            raise RuntimeError("SESSION_STRING пустая после очистки.")
        print(">> using USER session")
        client = TelegramClient(StringSession(ss), api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("USER session не авторизована.")
        return client
    if bot_token:
        print(">> using BOT token")
        client = TelegramClient("bot", api_id, api_hash)
        await client.start(bot_token=bot_token)
        return client
    raise RuntimeError("Нет TELEGRAM_SESSION/SESSION_STRING и TELEGRAM_BOT_TOKEN.")

# ---------- main ----------
async def backfill():
    api_id   = env_any("TELEGRAM_API_ID","API_ID", cast=int)
    api_hash = env_any("TELEGRAM_API_HASH","API_HASH")
    channel_cfg = env_any("CHANNEL_USERNAME","CHANNEL")
    sheet_id = env_any("GOOGLE_SHEETS_DB_ID","SHEET_ID")
    tab = env_any("LISTINGS_TAB", default="Listings")
    gsa_json = env_any("GOOGLE_SERVICE_ACCOUNT_JSON")
    limit = env_any("BACKFILL_LIMIT", default="1000")
    try: limit = int(limit)
    except: limit = 1000

    if not (api_id and api_hash): raise RuntimeError("Нет TELEGRAM_API_ID/API_ID или TELEGRAM_API_HASH/API_HASH.")
    if not channel_cfg:           raise RuntimeError("Нет CHANNEL_USERNAME/CHANNEL.")
    if not sheet_id:              raise RuntimeError("Нет GOOGLE_SHEETS_DB_ID/SHEET_ID.")
    if not gsa_json:              raise RuntimeError("Нет GOOGLE_SERVICE_ACCOUNT_JSON.")

    channel = normalize_channel(channel_cfg)
    ws = open_listings_ws(sheet_id, tab, gsa_json)

    existing_ids = set()
    try:
        for v in ws.col_values(3)[1:]:
            if v: existing_ids.add(v.strip())
    except Exception:
        pass

    print(f"==> Backfill started: channel={channel}, limit={limit}, known_ids={len(existing_ids)}")
    client = await build_client(api_id, api_hash)

    new_rows, saved_total = [], 0
    try:
        async for msg in client.iter_messages(channel, limit=limit):
            if not msg or not msg.id: continue
            mid = str(msg.id)
            if mid in existing_ids: continue

            text = (msg.message or "").strip() or (msg.caption or "").strip()
            price, br = parse_price_bedrooms(text or "")
            title = (text.splitlines()[0][:120] if text else "")
            ts = (msg.date or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")

            new_rows.append([ts,"channel",mid,str(channel_cfg),title,
                             price if price is not None else "",
                             br if br is not None else "", text])

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
        print("❗ Боту запрещено читать историю. Используй USER SESSION (SESSION_STRING).")
        raise
    finally:
        await client.disconnect()

    print("==> Backfill finished")

if __name__ == "__main__":
    asyncio.run(backfill())
