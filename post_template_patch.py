# -*- coding: utf-8 -*-
"""Richer standardized listing template and safe one-time migration."""
import html
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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
DONE_MARKER = "__STANDARDIZATION_DONE_V2__"
MEDIA_SELECTOR = ",".join([
    ".tgme_widget_message_photo_wrap", ".tgme_widget_message_video_player",
    ".tgme_widget_message_document_wrap", ".tgme_widget_message_sticker_wrap",
    ".tgme_widget_message_voice_player", ".tgme_widget_message_audio_player",
])


def _features(source, limit=280):
    out, seen = [], set()
    for raw in (source or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" •·—-\t")
        if len(line) < 4 or line.startswith("#") or SKIP_RE.search(line) or not FEATURE_RE.search(line):
            continue
        key = line.lower()
        if key in seen: continue
        seen.add(key); out.append(line)
        if len(out) >= 5: break
    text = " · ".join(out)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _hashtags(source):
    tags, seen = [], set()
    for tag in re.findall(r"(?<!\w)#[\w_]+", source or "", flags=re.UNICODE):
        low = tag.lower()
        if low not in seen:
            seen.add(low); tags.append(tag)
        if len(tags) >= 8: break
    return " ".join(tags)


def _crawl_payloads(mod, channel, target_ids, max_pages=450):
    target = {str(x) for x in target_ids if str(x)}
    links_by_mid, texts_by_mid, mode_by_mid = {}, {}, {}
    before = ""
    for page_no in range(1, max_pages + 1):
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        r = mod.requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status(); soup = mod.BeautifulSoup(r.text, "html.parser"); mids = []
        for msg in soup.select(".tgme_widget_message"):
            dp = (msg.get("data-post") or "").strip()
            if "/" not in dp: continue
            ch, mid = dp.rsplit("/", 1)
            if ch.lower() != channel.lower() or not mid.isdigit(): continue
            mids.append(int(mid))
            if mid not in target: continue
            node = msg.select_one(".tgme_widget_message_text")
            texts_by_mid[mid] = mod.html.unescape(node.get_text("\n", strip=True)).strip() if node else ""
            links = []
            if node:
                for a in node.select("a[href]"):
                    href = (a.get("href") or "").strip()
                    if href: links.append((href, a.get_text(" ", strip=True)))
            links_by_mid[mid] = links
            mode_by_mid[mid] = "caption" if msg.select_one(MEDIA_SELECTOR) else "text"
        if target.issubset(texts_by_mid.keys()) or not mids: break
        oldest = min(mids)
        if oldest <= 1 or str(oldest) == before: break
        before = str(oldest)
        if page_no % 20 == 0:
            mod.log.info("payload crawl @%s page=%s found=%s/%s", channel, page_no, len(texts_by_mid), len(target))
        time.sleep(.05)
    return links_by_mid, texts_by_mid, mode_by_mid


def _already_done(mod, catalog):
    try:
        vals = mod._backup_sheet(catalog).get_all_values()
        return any(r and r[0] == DONE_MARKER for r in vals)
    except Exception:
        return False


def _mark_done(mod, catalog, stats, failed_ids):
    try:
        ws = mod._backup_sheet(catalog)
        ws.append_row([
            DONE_MARKER, "", "", "", "",
            mod.json.dumps({"stats": stats, "failed_message_ids": failed_ids}, ensure_ascii=False),
            time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        ], value_input_option="RAW")
    except Exception:
        mod.log.exception("Could not write standardization DONE marker")


def apply(mod):
    def build_post(row, bot_username, links=None):
        lot = mod._shown(row.get("lot_id"), "—", 30); district = mod._shown(row.get("район")); typ = mod._shown(row.get("тип"))
        bedrooms = mod._shown(row.get("спальни"), "Не указано", 30); bathrooms = mod._shown(row.get("ванные"), "Не указано", 30)
        availability = mod._shown(row.get("доступность"), "Не указано", 70); electricity = mod._shown(row.get("электричество"), "Не указано", 55); water = mod._shown(row.get("вода"), "Не указано", 55)
        desc = mod._shown(row.get("описание"), "", 230) or "Подробности по объекту уточняйте у менеджера Cozy Asia."
        source = str(row.get("исходный_текст") or ""); details = _features(source); tags = _hashtags(source); bot = bot_username.lstrip("@")
        rent = f"https://t.me/{bot}?start=rent_{lot}" if lot != "—" else f"https://t.me/{bot}?start=rent"; search = f"https://t.me/{bot}?start=search"

        def compose(desc_text, details_text, tags_text):
            lines = [
                f"🏡 <b>ЛОТ №{mod._esc(lot)}</b>", f"📍 <b>{mod._esc(district)}</b> · 🏠 {mod._esc(typ)}",
                f"🛏 Спальни: {mod._esc(bedrooms)} · 🛁 Ванные: {mod._esc(bathrooms)}",
                f"🏊 Бассейн: {mod._esc(mod._pool(row))} · 🐾 Питомцы: {mod._esc(mod._yesno(row.get('питомцы')))}", "",
                "💰 <b>Условия аренды</b>", f"• Цена: <b>{mod._esc(mod._price(row))}</b>",
                f"• Депозит: {mod._esc(mod._money(row.get('депозит_thb')))} · Комиссия: {mod._esc(mod._money(row.get('комиссия_thb')))}",
                f"• 📅 {mod._esc(availability)}", f"• ⚡ {mod._esc(electricity)} · 💧 {mod._esc(water)}",
                f"• 🌊 До моря: {mod._esc(mod._distance(row.get('до_моря_м')))}", "", f"📝 <b>Описание:</b> {mod._esc(desc_text)}",
            ]
            if details_text: lines += ["", f"✨ <b>Дополнительно:</b> {mod._esc(details_text)}"]
            for title, href in mod._external_links(links, bot)[:2]: lines.append(f'<a href="{html.escape(href, quote=True)}">{mod._esc(title)}</a>')
            if tags_text: lines += ["", mod._esc(tags_text)]
            lines += ["", f'📝 <b>Оставить заявку — <a href="{rent}">ЖМИ ЗДЕСЬ</a></b>', f'🤖 <b>Подобрать другие варианты — <a href="{search}">НАПИСАТЬ БОТУ</a></b>']
            return "\n".join(lines)

        text = compose(desc, details, tags); plain = re.sub(r"<[^>]+>", "", text)
        if len(plain) > 990 and details:
            details = details[:140].rstrip() + "…"; text = compose(desc, details, tags); plain = re.sub(r"<[^>]+>", "", text)
        if len(plain) > 990:
            desc = desc[:130].rstrip() + "…"; text = compose(desc, details, tags); plain = re.sub(r"<[^>]+>", "", text)
        if len(plain) > 990: text = compose(desc, details, "")
        return text

    def edit_exact(token, channel, mid, new_html, mode):
        common = {"chat_id": f"@{channel}", "message_id": mid, "parse_mode": "HTML"}
        order = ["caption", "text"] if mode == "caption" else ["text", "caption"]
        errors = []
        for kind in order:
            if kind == "caption": payload = mod._tg(token, "editMessageCaption", {**common, "caption": new_html})
            else: payload = mod._tg(token, "editMessageText", {**common, "text": new_html, "disable_web_page_preview": "true"})
            if payload.get("ok"): return "edited_" + kind, ""
            desc = str(payload.get("description") or ""); errors.append(kind + ": " + desc)
            if "message is not modified" in desc.lower(): return "unchanged", ""
        return "failed", " | ".join(errors)

    def standardize_existing(catalog):
        if _already_done(mod, catalog):
            mod.log.info("standardization already DONE for @%s; skipping historical pass", catalog.CATALOG_CHANNEL)
            return {"channel": catalog.CATALOG_CHANNEL, "skipped_done": True}
        token, username, can_edit, status = mod.bot_identity_and_rights(catalog.CATALOG_CHANNEL)
        mod.log.info("standardizer preflight @%s bot=@%s status=%s can_edit=%s", catalog.CATALOG_CHANNEL, username, status, can_edit)
        if not can_edit: raise RuntimeError(f"@{username} has no can_edit_messages in @{catalog.CATALOG_CHANNEL}")
        rows = [r for r in catalog.load_catalog_rows(True) if str(r.get("lot_id") or "").strip() and str(r.get("telegram_message_id") or "").strip()]
        by_mid = {str(r["telegram_message_id"]): r for r in rows}; rows = list(by_mid.values()); mids = list(by_mid)
        links_by_mid, texts_by_mid, mode_by_mid = _crawl_payloads(mod, catalog.CATALOG_CHANNEL, mids, catalog.MAX_PAGES)
        mod._backup_rows(catalog, rows, links_by_mid, texts_by_mid)
        stats = {"channel": catalog.CATALOG_CHANNEL, "total": len(rows), "edited": 0, "unchanged": 0, "failed": 0}; failed_ids = []

        def job(row):
            mid = str(row["telegram_message_id"]); new_html = build_post(row, username, links_by_mid.get(mid, []))
            result, err = edit_exact(token, catalog.CATALOG_CHANNEL, mid, new_html, mode_by_mid.get(mid, "caption"))
            return row, result, err

        done = 0
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = [ex.submit(job, row) for row in sorted(rows, key=lambda x: int(x.get("telegram_message_id") or 0))]
            for f in as_completed(futures):
                row, result, err = f.result(); done += 1
                if result.startswith("edited"): stats["edited"] += 1
                elif result == "unchanged": stats["unchanged"] += 1
                else:
                    stats["failed"] += 1; failed_ids.append(str(row.get("telegram_message_id") or ""))
                    mod.log.warning("standardize failed @%s mid=%s lot=%s error=%s", catalog.CATALOG_CHANNEL, row.get("telegram_message_id"), row.get("lot_id"), (err or "")[:360])
                if done % 10 == 0 or done == len(rows):
                    mod.log.info("standardize @%s %s/%s edited=%s unchanged=%s failed=%s", catalog.CATALOG_CHANNEL, done, len(rows), stats["edited"], stats["unchanged"], stats["failed"])
        _mark_done(mod, catalog, stats, failed_ids)
        mod.log.info("standardization DONE @%s failed_ids=%s", catalog.CATALOG_CHANNEL, failed_ids)
        return stats

    mod.build_post = build_post
    mod.standardize_existing = standardize_existing
