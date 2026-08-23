# -*- coding: utf-8 -*-
"""Richer standardized listing template: preserve factual amenities and hashtags."""
import html
import re

FEATURE_RE = re.compile(
    r"wifi|wi-fi|интернет|кухн|парков|стирал|кондиционер|сад|террас|уборк|бель|полотен|"
    r"спортзал|gym|саун|джакуз|мебел|охран|видеонаб|барбекю|bbq|балкон|вид на море|"
    r"sea view|генератор|посуд|холодиль|микроволн|телевиз|smart tv|рабоч|детск|"
    r"магазин|рынок|кафе|ресторан|включен|included|pool cleaning|прачеч|laundry",
    re.I,
)
SKIP_RE = re.compile(
    r"лот\s*[№#]?|цена|стоимост|депозит|залог|комисси|электрич|вод[аы]|питом|"
    r"спальн|ванн|бассейн|доступ|свобод|до моря|оставить заявку|жми здесь|"
    r"подобрать другие|оператор|cozy\s*asia|@cozy",
    re.I,
)


def _features(source, limit=280):
    out = []
    seen = set()
    for raw in (source or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" •·—-\t")
        if len(line) < 4 or line.startswith("#") or SKIP_RE.search(line):
            continue
        if not FEATURE_RE.search(line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= 5:
            break
    text = " · ".join(out)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _hashtags(source):
    tags = []
    seen = set()
    for tag in re.findall(r"(?<!\w)#[\w_]+", source or "", flags=re.UNICODE):
        low = tag.lower()
        if low not in seen:
            seen.add(low); tags.append(tag)
        if len(tags) >= 8:
            break
    return " ".join(tags)


def apply(mod):
    def build_post(row, bot_username, links=None):
        lot = mod._shown(row.get("lot_id"), "—", 30)
        district = mod._shown(row.get("район"))
        typ = mod._shown(row.get("тип"))
        bedrooms = mod._shown(row.get("спальни"), "Не указано", 30)
        bathrooms = mod._shown(row.get("ванные"), "Не указано", 30)
        availability = mod._shown(row.get("доступность"), "Не указано", 70)
        electricity = mod._shown(row.get("электричество"), "Не указано", 55)
        water = mod._shown(row.get("вода"), "Не указано", 55)
        desc = mod._shown(row.get("описание"), "", 230) or "Подробности по объекту уточняйте у менеджера Cozy Asia."
        source = str(row.get("исходный_текст") or "")
        details = _features(source)
        tags = _hashtags(source)
        bot = bot_username.lstrip("@")
        rent = f"https://t.me/{bot}?start=rent_{lot}" if lot != "—" else f"https://t.me/{bot}?start=rent"
        search = f"https://t.me/{bot}?start=search"

        def compose(desc_text, details_text, tags_text):
            lines = [
                f"🏡 <b>ЛОТ №{mod._esc(lot)}</b>",
                f"📍 <b>{mod._esc(district)}</b> · 🏠 {mod._esc(typ)}",
                f"🛏 Спальни: {mod._esc(bedrooms)} · 🛁 Ванные: {mod._esc(bathrooms)}",
                f"🏊 Бассейн: {mod._esc(mod._pool(row))} · 🐾 Питомцы: {mod._esc(mod._yesno(row.get('питомцы')))}",
                "",
                "💰 <b>Условия аренды</b>",
                f"• Цена: <b>{mod._esc(mod._price(row))}</b>",
                f"• Депозит: {mod._esc(mod._money(row.get('депозит_thb')))} · Комиссия: {mod._esc(mod._money(row.get('комиссия_thb')))}",
                f"• 📅 {mod._esc(availability)}",
                f"• ⚡ {mod._esc(electricity)} · 💧 {mod._esc(water)}",
                f"• 🌊 До моря: {mod._esc(mod._distance(row.get('до_моря_м')))}",
                "",
                f"📝 <b>Описание:</b> {mod._esc(desc_text)}",
            ]
            if details_text:
                lines += ["", f"✨ <b>Дополнительно:</b> {mod._esc(details_text)}"]
            for title, href in mod._external_links(links, bot)[:2]:
                lines.append(f'<a href="{html.escape(href, quote=True)}">{mod._esc(title)}</a>')
            if tags_text:
                lines += ["", mod._esc(tags_text)]
            lines += [
                "",
                f'📝 <b>Оставить заявку — <a href="{rent}">ЖМИ ЗДЕСЬ</a></b>',
                f'🤖 <b>Подобрать другие варианты — <a href="{search}">НАПИСАТЬ БОТУ</a></b>',
            ]
            return "\n".join(lines)

        text = compose(desc, details, tags)
        plain = re.sub(r"<[^>]+>", "", text)
        if len(plain) > 990 and details:
            details = details[:140].rstrip() + "…"
            text = compose(desc, details, tags)
            plain = re.sub(r"<[^>]+>", "", text)
        if len(plain) > 990:
            desc = desc[:130].rstrip() + "…"
            text = compose(desc, details, tags)
            plain = re.sub(r"<[^>]+>", "", text)
        if len(plain) > 990:
            text = compose(desc, details, "")
        return text

    mod.build_post = build_post
