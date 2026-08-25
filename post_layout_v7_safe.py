# -*- coding: utf-8 -*-
"""V7: channel-safe Cozy Asia layout using ordinary Unicode emoji/text only."""
from __future__ import annotations

import html
import re
import post_template_patch as tpl


def _clean_details(value: str) -> str:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    s = re.sub(r"^(?:✨\s*)?(?:Дополнительно\s*:\s*)+", "", s, flags=re.I)
    return s.strip(" ·")


def apply(mod, throttle):
    mod.RUN_EXISTING = True
    mod.MARKER = "🏡 ЛОТ №"
    throttle.DONE_MARKER = "__STANDARDIZATION_DONE_V7_SAFE__"
    throttle.OK_PREFIX = "__STD_V7_SAFE__:"
    throttle.EXC_PREFIX = "__STD_V7_SAFE_EXCEPTION__:"

    def build_post(row, bot_username, links=None):
        lot = mod._shown(row.get("lot_id"), "—", 30)
        district = mod._shown(row.get("район"))
        typ = mod._shown(row.get("тип"))
        bedrooms = mod._shown(row.get("спальни"), "Не указано", 30)
        bathrooms = mod._shown(row.get("ванные"), "Не указано", 30)
        availability = mod._shown(row.get("доступность"), "Не указано", 70)
        electricity = mod._shown(row.get("электричество"), "Не указано", 55)
        water = mod._shown(row.get("вода"), "Не указано", 55)
        desc = mod._shown(row.get("описание"), "", 235) or "Подробности по объекту уточняйте у менеджера Cozy Asia."
        source = str(row.get("исходный_текст") or "")
        details = _clean_details(tpl._features(source))
        tags = tpl._hashtags(source)
        bot = bot_username.lstrip("@")
        rent = f"https://t.me/{bot}?start=rent_{lot}" if lot != "—" else f"https://t.me/{bot}?start=rent"
        search = f"https://t.me/{bot}?start=search"

        def compose(desc_text: str, details_text: str, tags_text: str) -> str:
            lines = [
                f"🏡 <b>ЛОТ №{mod._esc(lot)}</b>",
                "",
                "💬 <b>ОПИСАНИЕ</b>",
                f"<blockquote>{mod._esc(desc_text)}</blockquote>",
                "",
                f"📍 Район: {mod._esc(district)}",
                f"🏠 Тип: {mod._esc(typ)}",
                f"🛏 Спальни: {mod._esc(bedrooms)}",
                f"🛁 Ванные: {mod._esc(bathrooms)}",
                f"🏊 Бассейн: {mod._esc(mod._pool(row))}",
                f"🐾 Питомцы: {mod._esc(mod._yesno(row.get('питомцы')))}",
                "",
                "💰 <b>Условия аренды</b>",
                f"💵 Цена: {mod._esc(mod._price(row))}",
                f"🔐 Депозит: {mod._esc(mod._money(row.get('депозит_thb')))}",
                f"🤝 Комиссия: {mod._esc(mod._money(row.get('комиссия_thb')))}",
                f"📅 Доступность: {mod._esc(availability)}",
                f"⚡ Электричество: {mod._esc(electricity)}",
                f"💧 Вода: {mod._esc(water)}",
                f"🌊 До моря: {mod._esc(mod._distance(row.get('до_моря_м')))}",
            ]
            if details_text:
                lines += ["", f"✨ Дополнительно: {mod._esc(details_text)}"]
            for title, href in mod._external_links(links, bot)[:2]:
                lines.append(f'<a href="{html.escape(href, quote=True)}">{mod._esc(title)}</a>')
            if tags_text:
                lines += ["", mod._esc(tags_text)]
            lines += [
                "",
                "📝 <b>ОСТАВИТЬ ЗАЯВКУ</b>",
                f'👉 <a href="{html.escape(rent, quote=True)}"><b>ЖМИ ЗДЕСЬ</b></a> 👈',
                "",
                f'🔎🏡 ПОДОБРАТЬ ДРУГИЕ ВАРИАНТЫ — <a href="{html.escape(search, quote=True)}"><b>НАПИСАТЬ БОТУ</b></a> 🤖',
            ]
            return "\n".join(lines)

        text = compose(desc, details, tags)
        plain = re.sub(r"<[^>]+>", "", text)
        if len(plain) > 990 and details:
            details = details[:120].rstrip() + "…"
            text = compose(desc, details, tags)
            plain = re.sub(r"<[^>]+>", "", text)
        if len(plain) > 990:
            desc = desc[:135].rstrip() + "…"
            text = compose(desc, details, tags)
            plain = re.sub(r"<[^>]+>", "", text)
        if len(plain) > 990:
            text = compose(desc, details, "")
        return text

    mod.build_post = build_post
