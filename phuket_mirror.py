# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("phuket-mirror")

SOURCE_CHANNEL = os.environ.get("PHUKET_SOURCE_CHANNEL", "thailandsell").strip().lstrip("@")
DEST_CHANNEL = os.environ.get("PHUKET_DEST_CHANNEL", "phuket_developer").strip().lstrip("@")
CONTACT = os.environ.get("PHUKET_CONTACT", "@Cozy_asia").strip()
MODEL = os.environ.get("PHUKET_MIRROR_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini")).strip()
ENABLED = os.environ.get("PHUKET_MIRROR_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
AUTOJOIN = os.environ.get("PHUKET_SOURCE_AUTOJOIN", "1").strip().lower() not in {"0", "false", "no", "off"}

_sheet_lock = asyncio.Lock()
_attached = False

PROMO_MARKERS = (
    "Планируете приобретение недвижимости",
    "⚡️Москва и МО",
    "Доп. скидка по промокоду",
    "‼️Пишите много комплексов",
    "Аренда/Продажа/ Инвестиции подберем",
)

PHUKET_TERMS = (
    "пхукет", "phuket", "банг тао", "bang tao", "bangtao", "сурин", "surin",
    "най харн", "nai harn", "найянг", "nai yang", "камала", "kamala",
    "лаян", "layan", "чалонг", "chalong", "раваи", "rawai",
)

REALTY_TERMS = (
    "апартамент", "квартир", "кондо", "condo", "вилл", "недвижим",
    "застрой", "developer", "проект", "инвест", "freehold", "leasehold",
    "доходност", "рассроч", "ипотек", "юнит", "unit",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:20]


def _strip_source_promo(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    cuts = [text.find(marker) for marker in PROMO_MARKERS if text.find(marker) >= 0]
    if cuts:
        text = text[: min(cuts)]
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if re.fullmatch(r"(?:#[\wА-Яа-яЁё]+\s*){2,}", s):
            continue
        if "t.me/thailandsell" in s.lower() or "@vladvesi" in s.lower():
            continue
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _compact_numeric_text(text: str) -> str:
    text = (text or "").replace("\u00a0", " ").replace("\u202f", " ")
    for _ in range(3):
        text = re.sub(r"(?<=\d)\s(?=\d{3}(?:\D|$))", "", text)
    return text


def _numeric_facts(text: str) -> list[str]:
    compact = _compact_numeric_text(text)
    vals = re.findall(r"(?<![\w])\d+(?:[.,]\d+)?", compact)
    return sorted(v.replace(",", ".") for v in vals)


def _looks_relevant(body: str) -> bool:
    low = (body or "").lower()
    return any(x in low for x in PHUKET_TERMS) and any(x in low for x in REALTY_TERMS)


def _sheet(catalog):
    sh = catalog._client().open_by_key(catalog.SHEET_ID)
    try:
        return sh.worksheet("PhuketMirror")
    except Exception:
        ws = sh.add_worksheet(title="PhuketMirror", rows=2000, cols=9)
        ws.append_row(
            [
                "source_id",
                "grouped_id",
                "dest_ids",
                "source_hash",
                "status",
                "kind",
                "title",
                "updated_at",
                "note",
            ],
            value_input_option="RAW",
        )
        return ws


def _state_find_sync(catalog, source_id: int) -> dict | None:
    try:
        ws = _sheet(catalog)
        rows = ws.get_all_records()
        for row in reversed(rows):
            if str(row.get("source_id", "")).strip() == str(source_id):
                return row
    except Exception:
        log.exception("PhuketMirror state lookup failed source_id=%s", source_id)
    return None


def _state_save_sync(
    catalog,
    source_id: int,
    grouped_id: str,
    dest_ids: list[int],
    source_hash: str,
    status: str,
    kind: str,
    title: str,
    note: str = "",
):
    try:
        ws = _sheet(catalog)
        ws.append_row(
            [
                str(source_id),
                str(grouped_id or ""),
                ",".join(str(x) for x in dest_ids),
                source_hash,
                status,
                kind,
                title[:300],
                _now(),
                note[:500],
            ],
            value_input_option="RAW",
        )
    except Exception:
        log.exception("PhuketMirror state save failed source_id=%s", source_id)


async def _state_find(catalog, source_id: int) -> dict | None:
    async with _sheet_lock:
        return await asyncio.to_thread(_state_find_sync, catalog, source_id)


async def _state_save(catalog, *args, **kwargs):
    async with _sheet_lock:
        return await asyncio.to_thread(_state_save_sync, catalog, *args, **kwargs)


def _rewrite_sync(body: str, max_chars: int) -> dict:
    from openai import OpenAI

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    client = OpenAI(
        api_key=api_key,
        project=os.environ.get("OPENAI_PROJECT", "").strip() or None,
        organization=os.environ.get("OPENAI_ORG", "").strip() or None,
        timeout=45,
    )

    system = f"""
Ты редактор Telegram-канала Cozy Asia о покупке недвижимости на Пхукете.
На входе публикация другого агентства. Верни ТОЛЬКО JSON.

Задача:
1) publish=true только если материал относится к недвижимости Пхукета и полезен покупателю/инвестору:
   новый проект, конкретное предложение, условия застройщика, подборка проектов или рыночная аналитика.
   Самуи-only, аренду-only, вакансии, мероприятия и посторонний контент пропускай.
2) Полностью перепиши рекламные и описательные формулировки своим языком. Текст должен выглядеть самостоятельным.
3) НЕЛЬЗЯ менять, округлять, пересчитывать или придумывать факты:
   цены, валюты, площади, проценты, сроки, даты, расстояния, количество объектов/этажей,
   график платежей, ownership/freehold/leasehold, названия проектов и застройщиков.
4) Все числовые факты из исходного содержательного текста должны сохраниться. Не добавляй новых чисел.
5) Не копируй контакты, промокоды, ссылки, название агентства и CTA источника.
6) Допустимы уместные эмодзи, короткие подзаголовки и списки.
7) Русский язык. Максимум {max_chars} символов с учетом заголовка и основного текста.
8) Не говори, что это рерайт, копия или материал другого канала.

JSON:
{{
  "publish": true/false,
  "kind": "listing|project|analytics|selection|other",
  "title": "короткий авторский заголовок",
  "text": "полностью готовый основной текст без нашего CTA и хэштегов"
}}
""".strip()

    resp = client.chat.completions.create(
        model=MODEL,
        temperature=0.35,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": body},
        ],
        max_tokens=1800,
    )
    raw = (resp.choices[0].message.content or "").strip()
    data = json.loads(raw)
    return {
        "publish": bool(data.get("publish")),
        "kind": str(data.get("kind") or "other").strip().lower(),
        "title": str(data.get("title") or "").strip(),
        "text": str(data.get("text") or "").strip(),
    }


async def _rewrite(body: str, max_chars: int) -> dict:
    source_nums = _numeric_facts(body)
    last_error = ""
    for attempt in range(2):
        try:
            data = await asyncio.to_thread(_rewrite_sync, body, max_chars)
            if not data["publish"]:
                return data
            combined = (data["title"] + "\n" + data["text"]).strip()
            if _numeric_facts(combined) == source_nums:
                return data
            last_error = (
                f"numeric-facts mismatch source={source_nums} "
                f"output={_numeric_facts(combined)}"
            )
            log.warning("Phuket rewrite validation attempt=%s: %s", attempt + 1, last_error)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            log.exception("Phuket rewrite failed attempt=%s", attempt + 1)
    raise RuntimeError(last_error or "rewrite validation failed")


def _final_text(data: dict) -> str:
    title = data.get("title", "").strip()
    body = data.get("text", "").strip()
    cta = f"📩 Актуальный прайс, планировки и условия покупки — {CONTACT}"
    tags = "#Пхукет #НедвижимостьПхукет #Инвестиции #Таиланд"
    return f"{title}\n\n{body}\n\n{cta}\n\n{tags}".strip()


async def _download_media(client, messages, folder: str) -> list[str]:
    paths: list[str] = []
    for msg in messages:
        if not getattr(msg, "media", None):
            continue
        try:
            path = await client.download_media(msg, file=folder)
            if path and Path(path).is_file():
                paths.append(str(path))
        except Exception:
            log.exception("Could not download Phuket source media message_id=%s", getattr(msg, "id", None))
    return paths


async def _send_with_media(client, dest, files: list[str], caption: str) -> list[int]:
    sent_ids: list[int] = []
    if not files:
        sent = await client.send_message(dest, caption, link_preview=False)
        return [int(sent.id)]

    first = True
    for start in range(0, len(files), 10):
        chunk = files[start : start + 10]
        result = await client.send_file(
            dest,
            chunk if len(chunk) > 1 else chunk[0],
            caption=caption if first else None,
            supports_streaming=True,
        )
        first = False
        if isinstance(result, list):
            sent_ids.extend(int(x.id) for x in result)
        else:
            sent_ids.append(int(result.id))
    return sent_ids


async def _process_messages(client, catalog, dest, messages):
    messages = sorted(messages, key=lambda x: int(x.id))
    if not messages:
        return
    source_id = int(messages[0].id)
    grouped_id = str(getattr(messages[0], "grouped_id", "") or "")
    raw_text = next(
        (
            (getattr(m, "raw_text", None) or getattr(m, "message", None) or "").strip()
            for m in messages
            if (getattr(m, "raw_text", None) or getattr(m, "message", None) or "").strip()
        ),
        "",
    )
    body = _strip_source_promo(raw_text)
    source_hash = _sha(body)

    if not body:
        await _state_save(catalog, source_id, grouped_id, [], source_hash, "skipped", "other", "", "empty text")
        return

    old = await _state_find(catalog, source_id)
    if old and str(old.get("source_hash", "")).strip() == source_hash and str(old.get("status", "")).strip() == "published":
        return

    if not _looks_relevant(body):
        await _state_save(catalog, source_id, grouped_id, [], source_hash, "skipped", "other", "", "not Phuket realty")
        return

    me = await client.get_me()
    premium = bool(getattr(me, "premium", False))
    max_chars = 3400 if premium else 850

    try:
        data = await _rewrite(body, max_chars=max_chars)
    except Exception as e:
        await _state_save(
            catalog, source_id, grouped_id, [], source_hash, "error", "other", "", f"rewrite: {type(e).__name__}: {e}"
        )
        return

    if not data.get("publish"):
        await _state_save(
            catalog, source_id, grouped_id, [], source_hash, "skipped", data.get("kind", "other"), data.get("title", ""), "classifier"
        )
        return

    final = _final_text(data)
    if _numeric_facts(data["title"] + "\n" + data["text"]) != _numeric_facts(body):
        await _state_save(
            catalog, source_id, grouped_id, [], source_hash, "error", data.get("kind", "other"), data.get("title", ""), "post-validation mismatch"
        )
        return

    try:
        with tempfile.TemporaryDirectory(prefix="phuket_mirror_") as tmp:
            files = await _download_media(client, messages, tmp)
            dest_ids = await _send_with_media(client, dest, files, final)
        await _state_save(
            catalog,
            source_id,
            grouped_id,
            dest_ids,
            source_hash,
            "published",
            data.get("kind", "other"),
            data.get("title", ""),
            f"media={len(files)}",
        )
        log.info(
            "PhuketMirror published source=%s grouped=%s dest=%s kind=%s",
            source_id,
            grouped_id,
            dest_ids,
            data.get("kind"),
        )
    except Exception as e:
        log.exception("PhuketMirror publish failed source_id=%s", source_id)
        await _state_save(
            catalog,
            source_id,
            grouped_id,
            [],
            source_hash,
            "error",
            data.get("kind", "other"),
            data.get("title", ""),
            f"publish: {type(e).__name__}: {e}",
        )


async def attach(client, catalog):
    global _attached
    if _attached or not ENABLED:
        return
    _attached = True

    from telethon import events, functions
    from telethon.errors import UserAlreadyParticipantError

    source = await client.get_entity(SOURCE_CHANNEL)
    dest = await client.get_entity(DEST_CHANNEL)

    if AUTOJOIN:
        try:
            await client(functions.channels.JoinChannelRequest(source))
        except UserAlreadyParticipantError:
            pass
        except Exception:
            log.exception("Could not auto-join source @%s; continuing", SOURCE_CHANNEL)

    async def on_album(event):
        try:
            await _process_messages(client, catalog, dest, list(event.messages))
        except Exception:
            log.exception("PhuketMirror album handler failed")

    async def on_new(event):
        if getattr(event.message, "grouped_id", None):
            return
        try:
            await _process_messages(client, catalog, dest, [event.message])
        except Exception:
            log.exception("PhuketMirror message handler failed")

    client.add_event_handler(on_album, events.Album(chats=source))
    client.add_event_handler(on_new, events.NewMessage(chats=source))
    log.info(
        "PhuketMirror attached source=@%s destination=@%s autojoin=%s",
        SOURCE_CHANNEL,
        DEST_CHANNEL,
        AUTOJOIN,
    )
