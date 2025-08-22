# backfill_render.py — импорт ВСЕХ старых постов канала в лист Listings (Render-режим, без интерактива)
import os, re, json, time, sys
import gspread
from google.oauth2.service_account import Credentials
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError

LISTING_HEADERS = [
    "id","title","area","bedrooms","price_thb","distance_to_sea_m",
    "pets","available_from","available_to","link","message_id","status","notes"
]

LOT_RE   = re.compile(r"(?:Лот|Lot)[^\d]*(\d+)", re.I)
AREA_RE  = re.compile(r"(?:Район|Area)\s*[:\-]\s*([^\n]+)", re.I)
BEDS_RE  = re.compile(r"(?:спален|спальн|bedrooms?)\s*[:\-]?\s*(\d+)", re.I)
PRICE_RE = re.compile(r"(?:цена|price)\s*[:\-]?\s*([\d\s]+)", re.I)

def to_int(s, default=0):
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else default

def parse_listing_text(text: str) -> dict:
    t = text or ""
    lot = LOT_RE.search(t)
    area = AREA_RE.search(t)
    beds = BEDS_RE.search(t)
    price = PRICE_RE.search(t)
    return {
        "id": (lot.group(1) if lot else ""),
        "area": (area.group(1).strip() if area else ""),
        "bedrooms": to_int(beds.group(1) if beds else ""),
        "price_thb": to_int(price.group(1) if price else ""),
        "title": t.splitlines()[0][:120] if t else "Без названия",
        "status": "active",
    }

def _load_gsa():
    gsa = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not gsa:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    if gsa.startswith("{"):
        return json.loads(gsa)
    with open(gsa, "r", encoding="utf-8") as f:
        return json.load(f)

def open_listings_ws():
    creds = Credentials.from_service_account_info(
        _load_gsa(), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(os.environ["GOOGLE_SHEETS_DB_ID"])
    tab = os.getenv("LISTINGS_TAB", "Listings")
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(tab, rows=2000, cols=40)
        ws.append_row(LISTING_HEADERS)
    return ws

def get_existing_message_ids(ws):
    existing = set()
    for row in ws.get_all_records():
        mid = str(row.get("message_id","")).strip()
        if mid:
            existing.add(mid)
    return existing

def private_link_from_ids(chat_id: int, msg_id: int) -> str:
    # -100xxxxxxxxxx -> xxxxxxxxxx
    cid = str(abs(chat_id))
    if cid.startswith("100"):
        cid = cid[3:]
    return f"https://t.me/c/{cid}/{msg_id}"

def main():
    print("==> Backfill started")
    # ---- ENV (в Render укажем всё в Environment) ----
    api_id   = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    phone    = os.getenv("TELEGRAM_PHONE", "").strip()
    login_code = os.getenv("TELEGRAM_LOGIN_CODE", "").strip()        # одноразовый код из Telegram
    code_hash  = os.getenv("TELEGRAM_CODE_HASH", "").strip()         # хэш, который попросим сохранить при первом запуске
    twofa      = os.getenv("TELEGRAM_2FA_PASSWORD", "").strip()      # если включён 2FA
    channel    = os.environ.get("CHANNEL_USERNAME") or os.environ.get("CHANNEL")
    if not channel:
        raise RuntimeError("Set CHANNEL_USERNAME (без @) или CHANNEL")

    ws = open_listings_ws()
    existing = get_existing_message_ids(ws)

    client = TelegramClient("import_session", api_id, api_hash)  # файл сессии в контейнере

    with client:
        # --- без интерактива: если сессии нет, проводим вход через переменные окружения ---
        if not client.is_user_authorized():
            if not phone:
                raise RuntimeError("TELEGRAM_PHONE must be set (в формате +79990000000).")

            # Если кода нет — запрашиваем код, печатаем CODE_HASH и завершаем, чтобы вы добавили переменные и перезапустили.
            if not login_code:
                print("==> Sending login code to your Telegram...")
                sent = client.send_code_request(phone)
                print(f"==> CODE_HASH (сохраните в Environment как TELEGRAM_CODE_HASH): {sent.phone_code_hash}")
                print("==> Теперь добавьте TELEGRAM_LOGIN_CODE (из Telegram) и TELEGRAM_CODE_HASH, затем Redeploy.")
                sys.exit(1)

            # Если код есть, завершаем вход
            try:
                if code_hash:
                    client.sign_in(phone=phone, code=login_code, phone_code_hash=code_hash)
                else:
                    client.sign_in(phone=phone, code=login_code)
            except SessionPasswordNeededError:
                if not twofa:
                    raise RuntimeError("Включён 2FA. Укажите TELEGRAM_2FA_PASSWORD и Redeploy.")
                client.sign_in(password=twofa)
            except (PhoneCodeInvalidError, PhoneCodeExpiredError):
                raise RuntimeError("Код неверный/просрочен. Удалите TELEGRAM_LOGIN_CODE, Redeploy, получите новый CODE_HASH и код.")

        # --- импорт сообщений ---
        entity = client.get_entity(channel)
        chan_username = getattr(entity, "username", None)

        added = 0
        scanned = 0
        for msg in client.iter_messages(entity, limit=None):
            scanned += 1
            text = (getattr(msg, "text", None) or getattr(msg, "message", None) or getattr(msg, "caption", None) or "")
            if not text:
                continue
            mid = str(msg.id)
            if mid in existing:
                continue

            item = parse_listing_text(text)
            item["message_id"] = mid

            if chan_username:
                item["link"] = f"https://t.me/{chan_username}/{mid}"
            else:
                item["link"] = private_link_from_ids(msg.chat_id, msg.id)

            for k in LISTING_HEADERS:
                item.setdefault(k, "")

            ws.append_row([item.get(h,"") for h in LISTING_HEADERS])
            existing.add(mid)
            added += 1
            # бережно к API Sheets
            time.sleep(0.15)

        print(f"==> Done. Scanned: {scanned}, added: {added}")

if __name__ == "__main__":
    main()
