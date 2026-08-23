# -*- coding: utf-8 -*-
"""Standardize Cozy Asia channel listings while preserving external links and backups."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from telegram.ext import MessageHandler, filters

log = logging.getLogger("post-standardizer")
MARKER = "🤖 Подобрать другие варианты"
BACKUP_SHEET = os.getenv("POST_BACKUP_WORKSHEET_NAME", "PostBackup")
RUN_EXISTING = os.getenv("STANDARDIZE_EXISTING_ON_BOOT", "0").lower() in {"1", "true", "yes", "on"}
AUTO_FUTURE = os.getenv("STANDARDIZE_FUTURE_POSTS", "1").lower() not in {"0", "false", "no", "off"}


def _esc(v):
    return html.escape(str(v or "").strip(), quote=False)


def _shown(v, default="Не указано", maxlen=90):
    s = str(v or "").strip()
    if not s or s.lower() in {"unknown", "none", "null", "-", "—", "n/a"}:
        return default
    s = re.sub(r"\s+", " ", s)
    return s if len(s) <= maxlen else s[: maxlen - 1].rstrip() + "…"


def _money(v):
    s = str(v or "").strip().replace(" ", "")
    if not s:
        return "Не указано"
    try:
        return f"{int(float(s.replace(',', '.'))):,}".replace(",", " ") + " THB"
    except Exception:
        return _shown(v)


def _yesno(v):
    s = str(v or "").strip().lower()
    if s in {"yes", "да", "true", "1"}: return "Да"
    if s in {"no", "нет", "false", "0"}: return "Нет"
    return "Не указано"


def _pool(row):
    y = _yesno(row.get("бассейн"))
    if y != "Да": return y
    t = str(row.get("тип_бассейна") or "").strip().lower()
    names = {"private": "приватный", "shared": "общий", "infinity": "инфинити"}
    return "Да" + (f", {names.get(t, t)}" if t else "")


def _distance(v):
    s = str(v or "").strip()
    if not s: return "Не указано"
    try:
        n = int(float(s.replace(",", ".")))
        return "Первая линия / у моря" if n == 0 else f"{n} м"
    except Exception:
        return _shown(v)


def _price(row):
    m = row.get("цена_месяц_thb")
    d = row.get("цена_сутки_thb")
    parts = []
    if str(m or "").strip(): parts.append(_money(m) + "/мес")
    if str(d or "").strip(): parts.append(_money(d) + "/сутки")
    return " · ".join(parts) if parts else "Не указано"


def _external_links(links, bot_username=""):
    out, seen = [], set()
    bot_username = (bot_username or "").lower().lstrip("@")
    for href, label in links or []:
        href = (href or "").strip()
        if not href or href in seen: continue
        seen.add(href)
        low = href.lower()
        if low.startswith("tg://") or low.startswith("mailto:"): continue
        if "t.me/" in low:
            if bot_username and f"t.me/{bot_username}" in low: continue
            # Preserve unrelated Telegram links only if they are not the current post/channel navigation.
            if "/s/" in low or "?single" in low: continue
        host = urlparse(href).netloc.lower()
        if not host: continue
        lab = re.sub(r"\s+", " ", str(label or "").strip())
        if any(x in low for x in ("maps.app.goo.gl", "google.com/maps", "goo.gl/maps", "maps.google")):
            title = "📍 Локация на карте"
        elif any(x in host for x in ("drive.google.com", "disk.yandex", "yadi.sk", "photos.app.goo.gl")):
            title = "📸 Фото / дополнительные материалы"
        else:
            title = "🔗 " + (lab[:45] if lab and len(lab) > 2 else host)
        out.append((title, href))
        if len(out) >= 3: break
    return out


def build_post(row, bot_username, links=None):
    lot = _shown(row.get("lot_id"), "—", 30)
    district = _shown(row.get("район"))
    typ = _shown(row.get("тип"))
    bedrooms = _shown(row.get("спальни"), "Не указано", 30)
    bathrooms = _shown(row.get("ванные"), "Не указано", 30)
    pets = _yesno(row.get("питомцы"))
    availability = _shown(row.get("доступность"), "Не указано", 75)
    electricity = _shown(row.get("электричество"), "Не указано", 60)
    water = _shown(row.get("вода"), "Не указано", 60)
    desc = _shown(row.get("описание"), "", 210)
    if not desc:
        desc = "Подробности по объекту уточняйте у менеджера Cozy Asia."

    bot = bot_username.lstrip("@")
    rent = f"https://t.me/{bot}?start=rent_{lot}" if lot and lot != "—" else f"https://t.me/{bot}?start=rent"
    search = f"https://t.me/{bot}?start=search"

    lines = [
        f"🏡 <b>ЛОТ №{_esc(lot)}</b>",
        f"📍 Район: <b>{_esc(district)}</b>",
        f"🏠 Тип: {_esc(typ)}",
        f"🛏 Спальни: {_esc(bedrooms)} · 🛁 Ванные: {_esc(bathrooms)}",
        f"🏊 Бассейн: {_esc(_pool(row))} · 🐾 Питомцы: {_esc(pets)}",
        "",
        "💰 <b>Условия аренды</b>",
        f"Цена: <b>{_esc(_price(row))}</b>",
        f"Депозит: {_esc(_money(row.get('депозит_thb')))} · Комиссия: {_esc(_money(row.get('комиссия_thb')))}",
        f"📅 Доступность: {_esc(availability)}",
        f"⚡ Электричество: {_esc(electricity)} · 💧 Вода: {_esc(water)}",
        f"🌊 До моря: {_esc(_distance(row.get('до_моря_м')))}",
        "",
        f"📝 <b>Описание:</b> {_esc(desc)}",
    ]
    for title, href in _external_links(links, bot):
        lines.append(f'<a href="{html.escape(href, quote=True)}">{_esc(title)}</a>')
    lines += [
        "",
        f'📝 <b>Оставить заявку — <a href="{rent}">ЖМИ ЗДЕСЬ</a></b>',
        f'🤖 <b>Подобрать другие варианты — <a href="{search}">НАПИСАТЬ БОТУ</a></b>',
    ]
    text = "\n".join(lines)
    # Keep safely below Telegram caption limit. Shrink description first if required.
    plain = re.sub(r"<[^>]+>", "", text)
    if len(plain) > 990:
        excess = len(plain) - 970
        short = desc[: max(60, len(desc) - excess)].rstrip() + "…"
        text = text.replace(_esc(desc), _esc(short), 1)
    return text


def _tg(token, method, data, tries=5):
    url = f"https://api.telegram.org/bot{token}/{method}"
    for attempt in range(tries):
        r = requests.post(url, data=data, timeout=35)
        try: payload = r.json()
        except Exception: payload = {"ok": False, "description": r.text[:300]}
        if payload.get("ok"): return payload
        retry = ((payload.get("parameters") or {}).get("retry_after"))
        if retry:
            time.sleep(float(retry) + 1)
            continue
        if r.status_code >= 500 and attempt + 1 < tries:
            time.sleep(1 + attempt)
            continue
        return payload
    return {"ok": False, "description": "retry limit reached"}


def bot_identity_and_rights(channel):
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not token: raise RuntimeError("TELEGRAM_TOKEN missing")
    me = _tg(token, "getMe", {})
    if not me.get("ok"): raise RuntimeError("Telegram getMe failed")
    user = me["result"]
    username = user.get("username") or ""
    member = _tg(token, "getChatMember", {"chat_id": f"@{channel}", "user_id": user["id"]})
    if not member.get("ok"):
        raise RuntimeError("Cannot verify bot channel rights: " + str(member.get("description")))
    m = member["result"]
    can_edit = m.get("status") == "creator" or (m.get("status") == "administrator" and bool(m.get("can_edit_messages")))
    return token, username, can_edit, m.get("status")


def _crawl_links(channel, target_ids, max_pages=450):
    target = {str(x) for x in target_ids if str(x)}
    found = {}
    before = ""
    for page_no in range(1, max_pages + 1):
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        mids = []
        for msg in soup.select(".tgme_widget_message"):
            dp = (msg.get("data-post") or "").strip()
            if "/" not in dp: continue
            ch, mid = dp.rsplit("/", 1)
            if ch.lower() != channel.lower() or not mid.isdigit(): continue
            mids.append(int(mid))
            if mid not in target: continue
            node = msg.select_one(".tgme_widget_message_text")
            links = []
            if node:
                for a in node.select("a[href]"):
                    href = (a.get("href") or "").strip()
                    label = a.get_text(" ", strip=True)
                    if href: links.append((href, label))
            found[mid] = links
        if target.issubset(found.keys()) or not mids: break
        oldest = min(mids)
        if oldest <= 1 or str(oldest) == before: break
        before = str(oldest)
        if page_no % 20 == 0:
            log.info("link crawl @%s page=%s found=%s/%s", channel, page_no, len(found), len(target))
        time.sleep(.08)
    return found


def _backup_sheet(catalog):
    sh = catalog._client().open_by_key(catalog.SHEET_ID)
    try:
        ws = sh.worksheet(BACKUP_SHEET)
    except Exception:
        ws = sh.add_worksheet(title=BACKUP_SHEET, rows=1000, cols=8)
        ws.append_row(["message_id", "lot_id", "telegram_url", "original_text", "original_links_json", "standardized_text", "saved_at"], value_input_option="RAW")
    return ws


def _backup_rows(catalog, rows, links_by_mid, texts_by_mid):
    ws = _backup_sheet(catalog)
    existing = set()
    vals = ws.get_all_values()
    for r in vals[1:]:
        if r: existing.add(r[0])
    batch = []
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    for row in rows:
        mid = str(row.get("telegram_message_id") or "")
        if not mid or mid in existing: continue
        batch.append([
            mid, row.get("lot_id", ""), row.get("telegram_url", ""),
            texts_by_mid.get(mid, row.get("исходный_текст", "")),
            json.dumps(links_by_mid.get(mid, []), ensure_ascii=False), "", now,
        ])
    for i in range(0, len(batch), 50):
        ws.append_rows(batch[i:i+50], value_input_option="RAW")
    log.info("backup @%s added=%s", catalog.CATALOG_CHANNEL, len(batch))


def _crawl_texts(catalog, target_ids):
    # Reuse catalog crawler for visible text; it is enough for backup while links are saved separately.
    arr = catalog._crawl(catalog.CATALOG_CHANNEL, None, catalog.MAX_PAGES)
    wanted = {str(x) for x in target_ids}
    return {str(p["message_id"]): p.get("text", "") for p in arr if str(p["message_id"]) in wanted}


def _edit_one(token, channel, mid, new_html):
    common = {"chat_id": f"@{channel}", "message_id": mid, "parse_mode": "HTML"}
    cap = _tg(token, "editMessageCaption", {**common, "caption": new_html})
    if cap.get("ok"): return "edited_caption", ""
    desc = str(cap.get("description") or "")
    if "message is not modified" in desc.lower(): return "unchanged", ""
    # Text posts need editMessageText instead of caption.
    txt = _tg(token, "editMessageText", {**common, "text": new_html, "disable_web_page_preview": "true"})
    if txt.get("ok"): return "edited_text", ""
    desc2 = str(txt.get("description") or "")
    if "message is not modified" in desc2.lower(): return "unchanged", ""
    return "failed", desc2 or desc


def standardize_existing(catalog):
    token, username, can_edit, status = bot_identity_and_rights(catalog.CATALOG_CHANNEL)
    log.info("standardizer preflight @%s bot=@%s status=%s can_edit=%s", catalog.CATALOG_CHANNEL, username, status, can_edit)
    if not can_edit:
        raise RuntimeError(f"@{username} has no can_edit_messages in @{catalog.CATALOG_CHANNEL}")

    rows = [r for r in catalog.load_catalog_rows(True) if str(r.get("lot_id") or "").strip() and str(r.get("telegram_message_id") or "").strip()]
    # One row per Telegram message. Non-listing rows without a lot are deliberately excluded.
    by_mid = {str(r["telegram_message_id"]): r for r in rows}
    rows = list(by_mid.values())
    mids = list(by_mid)
    links_by_mid = _crawl_links(catalog.CATALOG_CHANNEL, mids, catalog.MAX_PAGES)
    texts_by_mid = _crawl_texts(catalog, mids)
    _backup_rows(catalog, rows, links_by_mid, texts_by_mid)

    stats = {"channel": catalog.CATALOG_CHANNEL, "total": len(rows), "edited": 0, "unchanged": 0, "failed": 0}
    for idx, row in enumerate(sorted(rows, key=lambda x: int(x.get("telegram_message_id") or 0))):
        mid = str(row["telegram_message_id"])
        new_html = build_post(row, username, links_by_mid.get(mid, []))
        result, err = _edit_one(token, catalog.CATALOG_CHANNEL, mid, new_html)
        if result.startswith("edited"): stats["edited"] += 1
        elif result == "unchanged": stats["unchanged"] += 1
        else:
            stats["failed"] += 1
            log.warning("standardize failed @%s mid=%s lot=%s error=%s", catalog.CATALOG_CHANNEL, mid, row.get("lot_id"), err[:220])
        if (idx + 1) % 10 == 0 or idx + 1 == len(rows):
            log.info("standardize @%s %s/%s edited=%s unchanged=%s failed=%s", catalog.CATALOG_CHANNEL, idx + 1, len(rows), stats["edited"], stats["unchanged"], stats["failed"])
        time.sleep(.18)
    return stats


def _message_links(msg):
    out = []
    try:
        mappings = []
        if getattr(msg, "entities", None): mappings.append(msg.parse_entities())
        if getattr(msg, "caption_entities", None): mappings.append(msg.parse_caption_entities())
        for mp in mappings:
            for ent, label in mp.items():
                typ = str(getattr(ent, "type", ""))
                if typ.endswith("text_link") and getattr(ent, "url", None): out.append((ent.url, label))
                elif typ.endswith("url") and label: out.append((label, label))
    except Exception:
        log.exception("Could not parse message entities")
    return out


async def standardize_future(catalog, update, context):
    if not AUTO_FUTURE: return
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat: return
    username = (getattr(chat, "username", None) or "").lower()
    if username != catalog.CATALOG_CHANNEL.lower(): return
    text = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()
    if not text or MARKER in text: return
    lot = catalog.extract_lot_id(text)
    if not lot or not catalog._is_listing(text, lot): return
    p = {"message_id": str(msg.message_id), "text": text, "published_at": "", "telegram_url": f"https://t.me/{catalog.CATALOG_CHANNEL}/{msg.message_id}"}
    try:
        row = await asyncio.to_thread(catalog._record, p, None)
        token, bot_username, can_edit, _ = await asyncio.to_thread(bot_identity_and_rights, catalog.CATALOG_CHANNEL)
        if not can_edit: return
        links = _message_links(msg)
        await asyncio.to_thread(_backup_rows, catalog, [row], {str(msg.message_id): links}, {str(msg.message_id): text})
        new_html = build_post(row, bot_username, links)
        await asyncio.sleep(1.0)
        result, err = await asyncio.to_thread(_edit_one, token, catalog.CATALOG_CHANNEL, str(msg.message_id), new_html)
        log.info("future standardize @%s mid=%s lot=%s result=%s", catalog.CATALOG_CHANNEL, msg.message_id, lot, result)
        if result == "failed": log.warning("future standardize error=%s", err[:220])
    except Exception:
        log.exception("future standardize failed mid=%s", getattr(msg, "message_id", "?"))


def install(app, catalog):
    async def handler(update, context):
        return await standardize_future(catalog, update, context)
    app.add_handler(MessageHandler(filters.ALL, handler), group=20)
    log.info("Post standardizer handler installed for @%s", catalog.CATALOG_CHANNEL)


def maybe_start_existing(catalog):
    if not RUN_EXISTING: return None
    return standardize_existing(catalog)
