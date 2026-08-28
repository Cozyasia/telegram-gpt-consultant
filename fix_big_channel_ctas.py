# -*- coding: utf-8 -*-
"""One-shot repair for the two legacy big-channel listings missing both CTA links.

This module is deliberately targeted. It preserves the existing caption text and
entities and only appends the two required CTA links to @cozy_asia_bot.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re

import requests

import cozy_catalog
import legacy_main as legacy
import mtproto_auth

log = logging.getLogger("fix-big-channel-ctas")
ENV = "FIX_BIG_CHANNEL_CTAS"
CHANNEL = "samuirental"
BOT = "cozy_asia_bot"
TARGETS = ((4817, "1172"), (4785, "1168"))


def enabled() -> bool:
    return os.getenv(ENV, "0").strip().lower() in {"1", "true", "yes", "on"}


def _u16(text: str) -> int:
    return len((text or "").encode("utf-16-le")) // 2


def _urls(entities) -> list[str]:
    return [str(getattr(e, "url", "") or "") for e in (entities or []) if getattr(e, "url", None)]


def _has(urls: list[str], payload: str) -> bool:
    if payload == "rent":
        return any(re.search(r"(?:^|[?&])start=rent(?:_[^&]+)?(?:&|$)", u, re.I) for u in urls)
    return any(re.search(r"(?:^|[?&])start=search(?:&|$)", u, re.I) for u in urls)


def _routed(url: str) -> bool:
    return f"t.me/{BOT.lower()}" in str(url or "").lower()


def _append(text: str, entities, lot: str):
    from telethon.tl.types import MessageEntityBold, MessageEntityTextUrl

    current = list(entities or [])
    urls = _urls(current)
    need_rent = not _has(urls, "rent")
    need_search = not _has(urls, "search")
    if not need_rent and not need_search:
        return text, current, False

    new_text = text.rstrip()
    new_entities = [copy.copy(e) for e in current]

    def add_line(prefix: str, anchor: str, url: str):
        nonlocal new_text
        if new_text:
            new_text += "\n"
        start_line = _u16(new_text)
        new_text += prefix + anchor
        anchor_py_start = len(new_text) - len(anchor)
        anchor_off = _u16(new_text[:anchor_py_start])
        new_entities.append(MessageEntityTextUrl(offset=anchor_off, length=_u16(anchor), url=url))
        # Keep the CTA visually strong without touching any existing entities.
        new_entities.append(MessageEntityBold(offset=start_line, length=_u16(prefix + anchor)))

    new_text += "\n"
    if need_rent:
        add_line(
            "📝 ОСТАВИТЬ ЗАЯВКУ — ",
            "ЖМИ ЗДЕСЬ",
            f"https://t.me/{BOT}?start=rent_{lot}",
        )
    if need_search:
        add_line(
            "🔎🏡 ПОДОБРАТЬ ДРУГИЕ ВАРИАНТЫ — ",
            "НАПИСАТЬ БОТУ",
            f"https://t.me/{BOT}?start=search",
        )
        new_text += " 🤖"

    new_entities.sort(key=lambda e: (int(getattr(e, "offset", 0)), int(getattr(e, "length", 0))))
    return new_text, new_entities, True


def _bot_entity(e):
    name = type(e).__name__
    base = {"offset": int(getattr(e, "offset", 0)), "length": int(getattr(e, "length", 0))}
    mapping = {
        "MessageEntityBold": "bold",
        "MessageEntityItalic": "italic",
        "MessageEntityUnderline": "underline",
        "MessageEntityStrike": "strikethrough",
        "MessageEntitySpoiler": "spoiler",
        "MessageEntityCode": "code",
        "MessageEntityUrl": "url",
        "MessageEntityMention": "mention",
        "MessageEntityHashtag": "hashtag",
        "MessageEntityBotCommand": "bot_command",
        "MessageEntityEmail": "email",
        "MessageEntityPhone": "phone_number",
        "MessageEntityCashtag": "cashtag",
    }
    if name in mapping:
        return {"type": mapping[name], **base}
    if name == "MessageEntityTextUrl":
        return {"type": "text_link", **base, "url": str(getattr(e, "url", "") or "")}
    if name == "MessageEntityCustomEmoji":
        return {"type": "custom_emoji", **base, "custom_emoji_id": str(getattr(e, "document_id", "") or "")}
    if name == "MessageEntityPre":
        return {"type": "pre", **base, "language": str(getattr(e, "language", "") or "")}
    if name == "MessageEntityBlockquote":
        typ = "expandable_blockquote" if bool(getattr(e, "collapsed", False)) else "blockquote"
        return {"type": typ, **base}
    return None


def _bot_api_edit(mid: int, text: str, entities, is_media: bool):
    token = str(legacy.TELEGRAM_TOKEN or "").strip()
    if not token:
        return {"ok": False, "description": "TELEGRAM_TOKEN missing"}
    me = requests.post(f"https://api.telegram.org/bot{token}/getMe", timeout=20).json()
    username = str((me.get("result") or {}).get("username") or "")
    if not me.get("ok") or username.lower() != BOT.lower():
        return {"ok": False, "description": f"Unexpected bot identity @{username}"}
    payload_entities = [x for x in (_bot_entity(e) for e in entities or []) if x]
    if is_media:
        method = "editMessageCaption"
        payload = {
            "chat_id": f"@{CHANNEL}",
            "message_id": mid,
            "caption": text,
            "caption_entities": json.dumps(payload_entities, ensure_ascii=False),
        }
    else:
        method = "editMessageText"
        payload = {
            "chat_id": f"@{CHANNEL}",
            "message_id": mid,
            "text": text,
            "entities": json.dumps(payload_entities, ensure_ascii=False),
            "disable_web_page_preview": "true",
        }
    return requests.post(f"https://api.telegram.org/bot{token}/{method}", data=payload, timeout=30).json()


async def _repair_one(client, channel, mid: int, lot: str):
    msg = await client.get_messages(channel, ids=mid)
    if not msg or not getattr(msg, "message", None):
        raise RuntimeError(f"Missing @{CHANNEL}/{mid}")
    text, entities, changed = _append(msg.message, msg.entities or [], lot)
    if not changed:
        urls = _urls(msg.entities or [])
        if _has(urls, "rent") and _has(urls, "search") and all(_routed(u) for u in urls if "start=" in u):
            return {"message_id": mid, "lot": lot, "result": "already_correct"}
        raise RuntimeError(f"Existing CTA routing is not valid for @{CHANNEL}/{mid}")

    mt_error = ""
    try:
        # Passing the Message object lets Telethon retain the exact peer context.
        await client.edit_message(msg, text, formatting_entities=entities, link_preview=False)
    except Exception as exc:
        mt_error = f"{type(exc).__name__}: {exc}"
        log.warning("MTProto repair failed mid=%s: %s; trying owning bot API", mid, mt_error)
        bot_result = await asyncio.to_thread(_bot_api_edit, mid, text, entities, bool(getattr(msg, "media", None)))
        if not bot_result.get("ok"):
            raise RuntimeError(f"MTProto={mt_error}; BotAPI={bot_result.get('description')}")

    await asyncio.sleep(1.2)
    verify = await client.get_messages(channel, ids=mid)
    urls = _urls(getattr(verify, "entities", None) or [])
    if not _has(urls, "rent") or not _has(urls, "search"):
        raise RuntimeError(f"CTA read-back missing @{CHANNEL}/{mid}")
    bad = [u for u in urls if "start=" in u and not _routed(u)]
    if bad:
        raise RuntimeError(f"Wrong bot remains @{CHANNEL}/{mid}: {bad}")
    return {"message_id": mid, "lot": lot, "result": "corrected", "mtproto_fallback": bool(mt_error)}


async def run():
    if not enabled():
        return {"enabled": False}
    client = await mtproto_auth.new_client(cozy_catalog)
    if not client:
        raise RuntimeError("Big-channel MTProto session is not authorized")
    try:
        channel = await client.get_entity(CHANNEL)
        results = []
        for mid, lot in TARGETS:
            results.append(await _repair_one(client, channel, mid, lot))
        log.info("FIX_BIG_CHANNEL_CTAS_DONE %s", json.dumps(results, ensure_ascii=False))
        return {"enabled": True, "results": results}
    finally:
        await client.disconnect()
