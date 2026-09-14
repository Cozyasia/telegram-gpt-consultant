# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import phuket_mirror

log = logging.getLogger("phuket-story")

ENABLED = os.environ.get("PHUKET_STORIES_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off", ""}
CONTACT = os.environ.get("PHUKET_CONTACT", "@Cozy_asia").strip()
_attached = False
_sheet_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sheet(catalog):
    sh = catalog._client().open_by_key(catalog.SHEET_ID)
    try:
        return sh.worksheet("PhuketStories")
    except Exception:
        ws = sh.add_worksheet(title="PhuketStories", rows=2000, cols=6)
        ws.append_row(
            ["source_id", "dest_id", "status", "random_id", "updated_at", "note"],
            value_input_option="RAW",
        )
        return ws


def _find_sync(catalog, source_id: int) -> dict | None:
    try:
        rows = _sheet(catalog).get_all_records()
        for row in reversed(rows):
            if str(row.get("source_id", "")).strip() == str(source_id):
                return row
    except Exception:
        log.exception("PhuketStories state lookup failed source=%s", source_id)
    return None


def _save_sync(catalog, source_id: int, dest_id: int, status: str, random_id: int | str = "", note: str = ""):
    try:
        _sheet(catalog).append_row(
            [str(source_id), str(dest_id or ""), status, str(random_id or ""), _now(), note[:500]],
            value_input_option="RAW",
        )
    except Exception:
        log.exception("PhuketStories state save failed source=%s", source_id)


async def _find(catalog, source_id: int):
    async with _sheet_lock:
        return await asyncio.to_thread(_find_sync, catalog, source_id)


async def _save(catalog, *args, **kwargs):
    async with _sheet_lock:
        return await asyncio.to_thread(_save_sync, catalog, *args, **kwargs)


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_width: int, max_lines: int = 4) -> str:
    words = (text or "").split()
    if not words:
        return ""
    lines: list[str] = []
    cur = words[0]
    for word in words[1:]:
        trial = f"{cur} {word}"
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = word
            if len(lines) >= max_lines - 1:
                break
    if len(lines) < max_lines:
        lines.append(cur)
    return "\n".join(lines[:max_lines])


def _pick_price_line(caption: str) -> str:
    for line in (caption or "").splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if any(x in low for x in ("thb", "бат", "฿", "цена", "стоимость", "от ")) and re.search(r"\d", s):
            return s[:100]
    return ""


def _make_vertical_story(source_path: str, caption: str, output_path: str):
    from PIL import Image, ImageDraw

    img = Image.open(source_path).convert("RGB")
    W, H = 1080, 1920
    scale = max(W / img.width, H / img.height)
    resized = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
    left = max(0, (resized.width - W) // 2)
    top = max(0, (resized.height - H) // 2)
    canvas = resized.crop((left, top, left + W, top + H))

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle((0, 0, W, 470), fill=(0, 0, 0, 120))
    od.rectangle((0, 1460, W, H), fill=(0, 0, 0, 135))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(canvas)

    lines = [x.strip() for x in (caption or "").splitlines() if x.strip()]
    title = lines[0] if lines else "Недвижимость на Пхукете"
    price = _pick_price_line(caption)

    title_font = _font(62, bold=True)
    price_font = _font(52, bold=True)
    small_font = _font(38, bold=False)

    title_wrapped = _wrap(draw, title, title_font, 920, max_lines=4)
    draw.multiline_text((80, 85), title_wrapped, font=title_font, fill="white", spacing=14)
    if price:
        draw.text((80, 1510), price, font=price_font, fill="white")
    draw.text((80, 1650), "Прайс • планировки • условия покупки", font=small_font, fill="white")
    draw.text((80, 1720), CONTACT, font=price_font, fill="white")

    canvas.convert("RGB").save(output_path, format="JPEG", quality=91, optimize=True)


async def _first_photo(client, messages, folder: str) -> str:
    for msg in sorted(messages, key=lambda x: int(x.id)):
        if not getattr(msg, "photo", None):
            continue
        try:
            path = await client.download_media(msg, file=folder)
            if path and Path(path).is_file():
                return str(path)
        except Exception:
            log.exception("Story source photo download failed message=%s", getattr(msg, "id", None))
    return ""


async def _publish_for(client, catalog, dest, messages):
    messages = sorted(messages, key=lambda x: int(x.id))
    if not messages:
        return
    source_id = int(messages[0].id)

    old = await _find(catalog, source_id)
    if old and str(old.get("status", "")).strip() == "published":
        return

    mirror_state = await phuket_mirror._state_find(catalog, source_id)
    if not mirror_state or str(mirror_state.get("status", "")).strip() != "published":
        return

    dest_ids = [int(x) for x in str(mirror_state.get("dest_ids", "")).split(",") if x.strip().isdigit()]
    if not dest_ids:
        return
    dest_id = dest_ids[0]

    try:
        from telethon import functions, helpers, types

        peer = await client.get_input_entity(dest)
        # This is the authoritative Telegram-side check for admin rights, boosts
        # and active-story limits. Any failure must not affect normal posts.
        await client(functions.stories.CanSendStoryRequest(peer=peer))

        dest_msg = await client.get_messages(dest, ids=dest_id)
        caption = (getattr(dest_msg, "raw_text", None) or getattr(dest_msg, "message", None) or "").strip()

        with tempfile.TemporaryDirectory(prefix="phuket_story_") as tmp:
            source_photo = await _first_photo(client, messages, tmp)
            if not source_photo:
                await _save(catalog, source_id, dest_id, "skipped", note="no source photo")
                return
            rendered = str(Path(tmp) / "story.jpg")
            await asyncio.to_thread(_make_vertical_story, source_photo, caption, rendered)
            uploaded = await client.upload_file(rendered)
            media = types.InputMediaUploadedPhoto(file=uploaded)
            random_id = helpers.generate_random_long()
            short_caption = f"Недвижимость на Пхукете • подробности {CONTACT}"
            await client(
                functions.stories.SendStoryRequest(
                    peer=peer,
                    media=media,
                    privacy_rules=[types.InputPrivacyValueAllowAll()],
                    random_id=random_id,
                    caption=short_caption,
                    period=86400,
                )
            )

        await _save(catalog, source_id, dest_id, "published", random_id=random_id)
        log.info("Phuket Story published source=%s dest=%s", source_id, dest_id)
    except Exception as e:
        # Expected examples: BOOSTS_REQUIRED, CHAT_ADMIN_REQUIRED,
        # PREMIUM_ACCOUNT_REQUIRED, STORIES_TOO_MUCH.
        log.warning("Phuket Story skipped source=%s: %s: %s", source_id, type(e).__name__, e)
        await _save(catalog, source_id, dest_id, "blocked", note=f"{type(e).__name__}: {e}")


async def attach(client, catalog):
    global _attached
    if _attached or not ENABLED:
        return
    _attached = True

    from telethon import events

    source = await client.get_entity(phuket_mirror.SOURCE_CHANNEL)
    dest = await client.get_entity(phuket_mirror.DEST_CHANNEL)

    async def on_album(event):
        await _publish_for(client, catalog, dest, list(event.messages))

    async def on_new(event):
        if getattr(event.message, "grouped_id", None):
            return
        await _publish_for(client, catalog, dest, [event.message])

    # Mirror handlers are registered first. Telethon dispatches handlers for the
    # same update in registration order, so the destination post/state normally
    # exists by the time the Story handler runs.
    client.add_event_handler(on_album, events.Album(chats=source))
    client.add_event_handler(on_new, events.NewMessage(chats=source))
    log.info("PhuketStories attached destination=@%s", phuket_mirror.DEST_CHANNEL)
