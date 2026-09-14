# -*- coding: utf-8 -*-
"""Runtime hook that attaches Phuket mirroring to the existing MTProto client.

This keeps a single Telegram MTProto connection for the Cozy Asia process instead
of creating a second listener with the same StringSession.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import tempfile

log = logging.getLogger("phuket-mirror-patch")

_applied = False
_catalog = None


async def _blocked_parallel_mtproto(update, context, *args, **kwargs):
    msg = getattr(update, "effective_message", None)
    if msg:
        await msg.reply_text(
            "Эта MTProto-команда временно отключена: активен постоянный PhuketMirror-клиент. "
            "Это защита Telegram-сессии от AUTH_KEY_DUPLICATED."
        )


def _env_on(name: str) -> bool:
    default = "1" if name in {"PHUKET_BACKFILL_LATEST_ON_START", "PHUKET_FORCE_LATEST_ROLLOUT"} else "0"
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _force_rewrite_sync(body: str, max_chars: int, phuket_mirror) -> dict:
    """Rewrite a single rollout-test post without applying the rental-only classifier."""
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
Ты редактор Telegram-канала Cozy Asia о недвижимости на Пхукете.
Это ОДНОРАЗОВЫЙ тест переноса уже выбранной публикации. Верни ТОЛЬКО JSON.

Полностью перепиши исходный текст своим языком и в аккуратном стиле Cozy Asia.
Не меняй и не пересчитывай ни одного факта: цены, валюты, площади, проценты,
сроки, даты, расстояния, количество объектов/этажей, графики платежей,
ownership/freehold/leasehold, названия проектов и застройщиков.
Все числовые факты исходного содержательного текста должны сохраниться и новых чисел добавлять нельзя.
Не копируй контакты, промокоды, ссылки, название агентства или CTA источника.
Русский язык. Уместны эмодзи, короткие подзаголовки и списки.
Максимум {max_chars} символов для title + text.

JSON:
{{"kind":"listing|project|analytics|selection|other","title":"короткий заголовок","text":"готовый основной текст"}}
""".strip()
    resp = client.chat.completions.create(
        model=phuket_mirror.MODEL,
        temperature=0.25,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": body}],
        max_tokens=1500,
    )
    data = json.loads((resp.choices[0].message.content or "{}").strip())
    result = {
        "publish": True,
        "kind": str(data.get("kind") or "other").strip().lower(),
        "title": str(data.get("title") or "").strip(),
        "text": str(data.get("text") or "").strip(),
    }
    combined = (result["title"] + "\n" + result["text"]).strip()
    if phuket_mirror._numeric_facts(combined) != phuket_mirror._numeric_facts(body):
        raise RuntimeError(
            f"numeric-facts mismatch source={phuket_mirror._numeric_facts(body)} "
            f"output={phuket_mirror._numeric_facts(combined)}"
        )
    return result


async def _force_publish_selected(client, catalog, dest, messages, phuket_mirror, phuket_story):
    messages = sorted(messages, key=lambda m: int(m.id))
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
    body = phuket_mirror._strip_source_promo(raw_text)
    if not body:
        raise RuntimeError("latest source post has no text after source-promo stripping")

    source_hash = phuket_mirror._sha(body)
    me = await client.get_me()
    max_chars = 1800 if bool(getattr(me, "premium", False)) else 850

    data = None
    last_error = None
    for attempt in range(2):
        try:
            data = await asyncio.to_thread(_force_rewrite_sync, body, max_chars, phuket_mirror)
            break
        except Exception as e:
            last_error = e
            log.warning("Forced Phuket rollout rewrite attempt=%s failed: %s", attempt + 1, e)
    if data is None:
        raise RuntimeError(f"forced rollout rewrite failed: {last_error}")

    final = phuket_mirror._final_text(data)
    with tempfile.TemporaryDirectory(prefix="phuket_force_rollout_") as tmp:
        files = await phuket_mirror._download_media(client, messages, tmp)
        dest_ids = await phuket_mirror._send_with_media(client, dest, files, final)

    await phuket_mirror._state_save(
        catalog,
        source_id,
        grouped_id,
        dest_ids,
        source_hash,
        "published",
        data.get("kind", "other"),
        data.get("title", ""),
        f"FORCED_ROLLOUT_TEST media={len(files)}",
    )
    log.info(
        "Phuket forced rollout published source=%s dest=%s media=%s",
        source_id,
        dest_ids,
        len(files),
    )

    if phuket_story.ENABLED:
        await phuket_story._publish_for(client, catalog, dest, messages)


async def _backfill_latest_once(client, catalog, phuket_mirror, phuket_story):
    """Process the newest source publication through the normal mirror pipeline."""
    source = await client.get_entity(phuket_mirror.SOURCE_CHANNEL)
    dest = await client.get_entity(phuket_mirror.DEST_CHANNEL)
    recent = await client.get_messages(source, limit=20)
    recent = [m for m in recent if getattr(m, "id", None)]
    if not recent:
        log.warning("Phuket latest backfill: source @%s has no messages", phuket_mirror.SOURCE_CHANNEL)
        return

    latest = recent[0]
    grouped_id = getattr(latest, "grouped_id", None)
    if grouped_id:
        messages = [m for m in recent if getattr(m, "grouped_id", None) == grouped_id]
    else:
        messages = [latest]

    messages = sorted(messages, key=lambda m: int(m.id))
    source_id = int(messages[0].id)
    log.info(
        "Phuket latest backfill selected source_id=%s grouped=%s media_items=%s",
        source_id,
        grouped_id or "",
        len(messages),
    )

    await phuket_mirror._process_messages(client, catalog, dest, messages)
    state = await phuket_mirror._state_find(catalog, source_id)
    log.info("Phuket latest backfill mirror state source_id=%s state=%s", source_id, state)

    status = str((state or {}).get("status", "")).strip()
    if status != "published" and _env_on("PHUKET_FORCE_LATEST_ROLLOUT"):
        log.info("Phuket latest rollout forcing source_id=%s after state=%s", source_id, status or "none")
        await _force_publish_selected(client, catalog, dest, messages, phuket_mirror, phuket_story)
    elif status == "published" and phuket_story.ENABLED:
        await phuket_story._publish_for(client, catalog, dest, messages)


def apply():
    global _applied
    if _applied:
        return

    import mtproto_auth
    import mtproto_premium
    import phuket_mirror
    import phuket_story
    from telethon import TelegramClient

    original_install = mtproto_premium.install
    original_run_until_disconnected = TelegramClient.run_until_disconnected

    mtproto_premium.cmd_test = _blocked_parallel_mtproto
    mtproto_premium.cmd_backfill = _blocked_parallel_mtproto
    mtproto_auth.status = _blocked_parallel_mtproto

    def patched_install(app, catalog):
        global _catalog
        _catalog = catalog
        return original_install(app, catalog)

    async def patched_run_until_disconnected(self, *args, **kwargs):
        if _catalog is not None and phuket_mirror.ENABLED:
            mirror_attached = False
            try:
                me = await self.get_me()
                source = await self.get_entity(phuket_mirror.SOURCE_CHANNEL)
                dest = await self.get_entity(phuket_mirror.DEST_CHANNEL)
                perms = await self.get_permissions(dest, me)
                is_creator = bool(getattr(perms, "is_creator", False))
                is_admin = bool(getattr(perms, "is_admin", False))
                post_messages = getattr(perms, "post_messages", None)
                can_post = is_creator or (is_admin and post_messages is not False)
                log.info(
                    "PhuketMirror preflight account=@%s source=@%s destination=@%s "
                    "creator=%s admin=%s post_messages=%s can_post=%s",
                    getattr(me, "username", "") or "unknown",
                    getattr(source, "username", "") or phuket_mirror.SOURCE_CHANNEL,
                    getattr(dest, "username", "") or phuket_mirror.DEST_CHANNEL,
                    is_creator,
                    is_admin,
                    post_messages,
                    can_post,
                )
                if not can_post:
                    raise RuntimeError(
                        f"Telegram account @{getattr(me, 'username', '') or 'unknown'} "
                        f"cannot post to @{phuket_mirror.DEST_CHANNEL}"
                    )
                await phuket_mirror.attach(self, _catalog)
                mirror_attached = True
            except Exception:
                log.exception("Could not attach PhuketMirror; base MTProto daemon continues")

            if phuket_story.ENABLED:
                try:
                    await phuket_story.attach(self, _catalog)
                except Exception:
                    log.exception("Could not attach PhuketStories; normal mirror continues")

            if mirror_attached and _env_on("PHUKET_BACKFILL_LATEST_ON_START"):
                try:
                    await _backfill_latest_once(self, _catalog, phuket_mirror, phuket_story)
                except Exception:
                    log.exception("Phuket latest backfill failed; live mirror continues")

        result = original_run_until_disconnected(self, *args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    mtproto_premium.install = patched_install
    TelegramClient.run_until_disconnected = patched_run_until_disconnected
    _applied = True
    log.info("PhuketMirror runtime hook installed")