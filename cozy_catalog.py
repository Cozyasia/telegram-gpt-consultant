# -*- coding: utf-8 -*-
"""Cozy Asia catalog: one Telegram channel -> one Google Sheet."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import threading
import unicodedata
from datetime import datetime, timezone
from typing import Dict, List, Tuple

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("cozy-catalog")

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDS_RAW = os.environ.get("GOOGLE_CREDS_JSON", "").strip()
LOTS_WORKSHEET_NAME = os.environ.get("LOTS_WORKSHEET_NAME", "Lots").strip() or "Lots"
CATALOG_CHANNEL = os.environ.get("CATALOG_CHANNEL", "samuirental").strip().lstrip("@")
CATALOG_BOOTSTRAP_LIMIT = int(os.environ.get("CATALOG_BOOTSTRAP_LIMIT", "20") or "20")
CATALOG_BOOTSTRAP_IMPORT = os.environ.get("CATALOG_BOOTSTRAP_IMPORT", "1").strip().lower() not in {"0", "false", "no", "off"}
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_PROJECT = os.environ.get("OPENAI_PROJECT", "").strip()
OPENAI_ORG = os.environ.get("OPENAI_ORG", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()

HEADERS = [
    "lot_id", "telegram_message_id", "telegram_url", "published_at", "status",
    "тип", "район", "спальни", "ванные", "бассейн", "тип_бассейна",
    "цена_месяц_thb", "цена_сутки_thb", "депозит_thb", "комиссия_thb",
    "до_моря_м", "доступность", "питомцы", "электричество", "вода",
    "контакт_собственника", "описание", "исходный_текст", "extracted_at",
    "confidence", "needs_review",
]

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _client():
    if not SHEET_ID or not GOOGLE_CREDS_RAW:
        raise RuntimeError("Google Sheets disabled: missing GOOGLE_SHEET_ID or GOOGLE_CREDS_JSON")
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(GOOGLE_CREDS_RAW)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def ensure_lots_sheet():
    with _lock:
        sh = _client().open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet(LOTS_WORKSHEET_NAME)
        except Exception:
            ws = sh.add_worksheet(title=LOTS_WORKSHEET_NAME, rows=2000, cols=26)
        vals = ws.get_all_values()
        if not vals:
            ws.append_row(HEADERS, value_input_option="RAW")
        else:
            head = list(vals[0])
            changed = False
            for h in HEADERS:
                if h not in head:
                    head.append(h)
                    changed = True
            if changed:
                ws.update("A1", [head], value_input_option="RAW")
        return ws


def _normalize_digits(text: str) -> str:
    s = (text or "").replace("\ufe0f", "").replace("\u20e3", "")
    s = s.replace("➖", "-").replace("–", "-").replace("—", "-").replace("−", "-")
    s = unicodedata.normalize("NFKC", s)
    out = []
    for ch in s:
        try:
            out.append(str(int(unicodedata.digit(ch))))
        except (TypeError, ValueError):
            out.append(ch)
    return "".join(out)


def extract_lot_id(text: str) -> str:
    raw = text or ""
    norm = _normalize_digits(raw)
    lines = [x.strip() for x in norm.splitlines()[:25] if x.strip()]
    head = "\n".join(lines[:12])

    for pat in [
        r"(?i)(?:лот|lot)\s*(?:№|#|no\.?)?\s*[:\-]?\s*(\d{3,7})",
        r"(?:№|#)\s*(\d{3,7})",
        r"(?<!\d)\d{1,2}\s*-\s*(\d{3,7})(?!\d)",
    ]:
        m = re.search(pat, head)
        if m:
            return m.group(1).lstrip("0") or "0"

    # Telegram sometimes renders emoji lot digits as separate lines:
    # 1️⃣\n1️⃣\n7️⃣\n6️⃣  -> 1176.
    run: List[str] = []
    for line in lines:
        if re.fullmatch(r"\d", line):
            run.append(line)
            if len(run) > 7:
                run = run[-7:]
        else:
            if 3 <= len(run) <= 7:
                return ("".join(run)).lstrip("0") or "0"
            run = []
    if 3 <= len(run) <= 7:
        return ("".join(run)).lstrip("0") or "0"

    if lines:
        original_head = "\n".join((raw.splitlines() or [""])[:12])
        decorative = "\u20e3" in original_head or "➖" in original_head or "🔤" in original_head
        if decorative:
            groups = re.findall(r"(?<!\d)(\d{3,7})(?!\d)", head)
            if groups:
                return groups[-1].lstrip("0") or "0"
    return ""


def _is_listing(text: str, lot_id: str = "") -> bool:
    if lot_id:
        return True
    t = (text or "").lower()
    housing = any(k in t for k in ("вилла", "дом", "апартамент", "квартира", "студия", "villa", "house", "apartment", "condo", "bungalow"))
    money = any(k in t for k in ("бат", "thb", "฿", "аренд", "стоимость", "цена"))
    return housing and money


def _fallback(text: str) -> Dict[str, str]:
    low = (text or "").lower()
    d = {h: "" for h in HEADERS}
    d.update({"lot_id": extract_lot_id(text), "бассейн": "unknown", "тип_бассейна": "unknown", "питомцы": "unknown", "confidence": "low", "needs_review": "yes"})
    if "вилла" in low or "villa" in low:
        d["тип"] = "вилла"
    elif "дом" in low or "house" in low:
        d["тип"] = "дом"
    elif any(x in low for x in ("апартамент", "квартир", "condo", "apartment")):
        d["тип"] = "апартаменты"
    m = re.search(r"(?i)(\d+)\s*(?:спальн|bedroom|br\b)", text or "")
    if m:
        d["спальни"] = m.group(1)
    if "бассейн" in low or "pool" in low:
        d["бассейн"] = "yes"
    return d


def _extract(text: str) -> Dict[str, str]:
    deterministic = extract_lot_id(text)
    if not OPENAI_API_KEY:
        return _fallback(text)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY, project=OPENAI_PROJECT or None, organization=OPENAI_ORG or None, timeout=45)
        system = """Ты извлекаешь факты из объявления Cozy Asia об аренде недвижимости на Самуи. Верни ТОЛЬКО JSON. Ничего не додумывай. Если факта нет — пустая строка или unknown. Не считай контакты Cozy Asia контактом собственника. контакт_собственника заполняй только если явно написано owner/landlord/собственник/хозяин. Числовые цены возвращай цифрами без пробелов и валюты. Ключи: lot_id, тип, район, спальни, ванные, бассейн, тип_бассейна, цена_месяц_thb, цена_сутки_thb, депозит_thb, комиссия_thb, до_моря_м, доступность, питомцы, электричество, вода, контакт_собственника, описание, confidence, needs_review."""
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
            response_format={"type": "json_object"}, temperature=0, max_tokens=900,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        out = {str(k): "" if v is None else str(v).strip() for k, v in data.items()}
    except Exception as e:
        log.warning("OpenAI extraction failed: %s", e)
        out = _fallback(text)

    if (out.get("lot_id") or "").strip().lower() in {"unknown", "none", "null", "-", "—"}:
        out["lot_id"] = ""
    if deterministic:
        out["lot_id"] = deterministic
    out.setdefault("confidence", "medium" if deterministic else "low")
    out.setdefault("needs_review", "no" if deterministic else "yes")
    return out


def _existing(ws) -> Tuple[Dict[str, Tuple[int, Dict[str, str]]], List[str]]:
    rows = ws.get_all_values()
    if not rows:
        return {}, HEADERS[:]
    headers = list(rows[0])
    found = {}
    for rn, row in enumerate(rows[1:], start=2):
        row = row + [""] * max(0, len(headers) - len(row))
        d = dict(zip(headers, row))
        mid = d.get("telegram_message_id", "").strip()
        if mid:
            found[mid] = (rn, d)
    return found, headers


def _make_record(text: str, message_id: str, telegram_url: str, published_at: str, old: Dict[str, str] | None = None) -> Dict[str, str]:
    parsed = _extract(text)
    rec = {h: "" for h in HEADERS}
    rec.update(parsed)
    rec.update({
        "telegram_message_id": str(message_id), "telegram_url": telegram_url,
        "published_at": published_at, "status": "active", "исходный_текст": text or "",
        "extracted_at": _now(),
    })
    if old:
        if old.get("контакт_собственника", "").strip():
            rec["контакт_собственника"] = old["контакт_собственника"].strip()
        if old.get("status", "").strip():
            rec["status"] = old["status"].strip()
    return rec


def _write_record(ws, headers: List[str], existing: Dict[str, Tuple[int, Dict[str, str]]], rec: Dict[str, str]) -> str:
    mid = rec["telegram_message_id"]
    current = existing.get(mid)
    row_values = [rec.get(h, "") for h in headers]
    if current:
        rn = current[0]
        ws.update(f"A{rn}", [row_values], value_input_option="USER_ENTERED")
        existing[mid] = (rn, rec.copy())
        return "updated"
    ws.append_row(row_values, value_input_option="USER_ENTERED")
    # Row number is only needed for a subsequent duplicate in this same import.
    existing[mid] = (-1, rec.copy())
    return "inserted"


def upsert_listing(*, text: str, message_id: str, telegram_url: str, published_at: str = "", force: bool = False) -> Dict[str, str]:
    with _lock:
        ws = ensure_lots_sheet()
        existing, headers = _existing(ws)
        current = existing.get(str(message_id))
        if current and not force and current[1].get("исходный_текст", "") == (text or ""):
            return {"action": "skipped", "lot_id": current[1].get("lot_id", "")}
        rec = _make_record(text, str(message_id), telegram_url, published_at, current[1] if current else None)
        action = _write_record(ws, headers, existing, rec)
        return {"action": action, "lot_id": rec.get("lot_id", "")}


def _parse_page(channel: str, before: str = "") -> List[Dict[str, str]]:
    url = f"https://t.me/s/{channel}"
    if before:
        url += f"?before={before}"
    r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for msg in soup.select(".tgme_widget_message"):
        data_post = (msg.get("data-post") or "").strip()
        if "/" not in data_post:
            continue
        ch, mid = data_post.rsplit("/", 1)
        if ch.lower() != channel.lower() or not mid.isdigit():
            continue
        node = msg.select_one(".tgme_widget_message_text")
        if node is None:
            continue
        text = html.unescape(node.get_text("\n", strip=True)).strip()
        if not text:
            continue
        time_node = msg.select_one("time")
        published = (time_node.get("datetime") or "").strip() if time_node else ""
        link = msg.select_one("a.tgme_widget_message_date")
        post_url = (link.get("href") or "").strip() if link else f"https://t.me/{channel}/{mid}"
        items.append({"message_id": mid, "text": text, "published_at": published, "telegram_url": post_url})
    return items


def _public_posts(channel: str, wanted: int) -> List[Dict[str, str]]:
    wanted = max(1, min(int(wanted), 50))
    by_id: Dict[str, Dict[str, str]] = {}
    before = ""
    for _ in range(12):
        page = _parse_page(channel, before)
        if not page:
            break
        added = 0
        for p in page:
            if p["message_id"] not in by_id:
                by_id[p["message_id"]] = p
                added += 1
        ordered = sorted(by_id.values(), key=lambda x: int(x["message_id"]))
        if len(ordered) >= wanted:
            return ordered[-wanted:]
        oldest = min(int(p["message_id"]) for p in page)
        next_before = str(oldest)
        if not added or next_before == before:
            break
        before = next_before
    ordered = sorted(by_id.values(), key=lambda x: int(x["message_id"]))
    return ordered[-wanted:]


def import_public_channel_latest(limit: int = 20, force: bool = False) -> Dict[str, object]:
    limit = max(1, min(int(limit or 20), 50))
    with _lock:
        ws = ensure_lots_sheet()
        existing, headers = _existing(ws)
        posts = _public_posts(CATALOG_CHANNEL, limit)
        stats = {"channel": CATALOG_CHANNEL, "inspected": len(posts), "listing_candidates": 0, "inserted": 0, "updated": 0, "skipped": 0, "needs_review": 0, "errors": 0, "lots": []}
        for p in posts:
            lot = extract_lot_id(p["text"])
            if not _is_listing(p["text"], lot):
                continue
            stats["listing_candidates"] += 1
            current = existing.get(p["message_id"])
            if current and not force and current[1].get("исходный_текст", "") == p["text"]:
                stats["skipped"] += 1
                old_lot = current[1].get("lot_id", "")
                if old_lot:
                    stats["lots"].append(old_lot)
                continue
            try:
                rec = _make_record(p["text"], p["message_id"], p["telegram_url"], p["published_at"], current[1] if current else None)
                action = _write_record(ws, headers, existing, rec)
                stats[action] += 1
                if rec.get("lot_id"):
                    stats["lots"].append(rec["lot_id"])
                if rec.get("needs_review", "").lower() == "yes":
                    stats["needs_review"] += 1
            except Exception:
                stats["errors"] += 1
                log.exception("Import failed for %s", p["message_id"])
        stats["lots"] = list(dict.fromkeys(stats["lots"]))
        return stats


def catalog_status() -> Dict[str, object]:
    ws = ensure_lots_sheet()
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return {"rows": 0, "last": []}
    headers = rows[0]
    data = []
    for row in rows[1:]:
        row = row + [""] * max(0, len(headers) - len(row))
        data.append(dict(zip(headers, row)))
    return {"rows": len(data), "last": [r.get("lot_id") or f"msg:{r.get('telegram_message_id')}" for r in data[-5:]]}


async def cmd_catalog_import(update, context):
    limit = CATALOG_BOOTSTRAP_LIMIT
    if getattr(context, "args", None):
        try:
            limit = int(context.args[0])
        except Exception:
            pass
    limit = max(1, min(limit, 50))
    await update.effective_message.reply_text(f"Импортирую последние {limit} публикаций из @{CATALOG_CHANNEL}…")
    try:
        s = await asyncio.to_thread(import_public_channel_latest, limit, False)
        await update.effective_message.reply_text(
            f"Каталог обновлён. Проверено: {s['inspected']}; объявлений: {s['listing_candidates']}; "
            f"добавлено: {s['inserted']}; обновлено: {s['updated']}; без изменений: {s['skipped']}; "
            f"ошибок: {s['errors']}. Лоты: {', '.join(s['lots'][:20]) or '—'}"
        )
    except Exception as e:
        log.exception("Manual catalog import failed")
        await update.effective_message.reply_text(f"Импорт не выполнен: {type(e).__name__}: {e}")


async def cmd_catalog_status(update, context):
    try:
        s = await asyncio.to_thread(catalog_status)
        await update.effective_message.reply_text(f"Каталог Lots: строк {s['rows']}. Последние: {', '.join(s['last']) or '—'}")
    except Exception as e:
        await update.effective_message.reply_text(f"Каталог недоступен: {type(e).__name__}: {e}")


async def catch_catalog_updates(update, context):
    msg = getattr(update, "channel_post", None) or getattr(update, "edited_channel_post", None)
    if msg is None:
        return
    chat = getattr(msg, "chat", None)
    username = (getattr(chat, "username", "") or "").lstrip("@")
    if username.lower() != CATALOG_CHANNEL.lower():
        return
    text = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()
    if not text:
        return
    lot = extract_lot_id(text)
    if not _is_listing(text, lot):
        return
    published = ""
    if getattr(msg, "date", None):
        try:
            published = msg.date.astimezone(timezone.utc).isoformat(timespec="seconds")
        except Exception:
            published = str(msg.date)
    try:
        await asyncio.to_thread(
            upsert_listing,
            text=text,
            message_id=str(msg.message_id),
            telegram_url=f"https://t.me/{CATALOG_CHANNEL}/{msg.message_id}",
            published_at=published,
            force=True,
        )
    except Exception:
        log.exception("Failed ingesting channel post %s", msg.message_id)
