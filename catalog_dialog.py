# -*- coding: utf-8 -*-
"""Conversational catalog search and voice input for Cozy Asia Telegram bot."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
from typing import Any, Dict, List, Tuple

log = logging.getLogger("catalog-dialog")

PAGE_SIZE = int(os.getenv("CATALOG_PAGE_SIZE", "5") or 5)
MAX_RESULTS = int(os.getenv("CATALOG_RESULT_POOL", "60") or 60)
STATE_TTL = int(os.getenv("CATALOG_STATE_TTL", "7200") or 7200)
TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1").strip() or "whisper-1"

_RESET_RE = re.compile(r"(?i)\b(?:новый\s+поиск|новый\s+запрос|сбрось\s+поиск|начн[её]м\s+заново)\b")
_PROPERTY_WORDS = re.compile(
    r"(?i)\b(?:вилл\w*|дом\w*|бунгал\w*|таунхаус\w*|апартамент\w*|квартир\w*|"
    r"студи\w*|кондо\w*|спальн\w*|бассейн\w*|pool|ламай|lamai|бопхут|bophut|"
    r"бо\s*пут|маенам|maenam|чавенг|chaweng|банграк|bangrak|плай\s*лаем|plai\s*laem|"
    r"липа\s*ной|lipa\s*noi|талинг\s*нгам|taling\s*ngam|натон|nathon|чонг\s*мон|choeng\s*mon|"
    r"бат|thb|бюджет|до\s+\d[\d\s]*\s*(?:тыс|бат|thb)|лот\s*[№#]?\s*\d)\b"
)

DISTRICT_ALIASES = {
    "Ламай": ("ламай", "лами", "lamai", "maret", "марет"),
    "Бопхут": ("бопхут", "бо пут", "бо-пут", "bophut", "bo phut"),
    "Маенам": ("маенам", "май нам", "мае нам", "maenam", "mae nam"),
    "Чавенг": ("чавенг", "chaweng"),
    "Чавенг Ной": ("чавенг ной", "chaweng noi"),
    "Банграк": ("банграк", "банг рак", "bangrak", "bang rak"),
    "Плай Лаем": ("плай лаем", "плай-лаем", "plai laem"),
    "Липа Ной": ("липа ной", "липа-ной", "lipa noi"),
    "Талинг Нгам": ("талинг нгам", "талинг нам", "taling ngam"),
    "Натон": ("натон", "nathon", "naton"),
    "Чонг Мон": ("чонг мон", "чонгмон", "choeng mon", "chong mon"),
    "Банг По": ("банг по", "bang po", "bang-po"),
    "На Муанг": ("на муанг", "na muang", "namuang"),
    "Хуа Танон": ("хуа танон", "hua thanon", "hua tanon", "huathanon"),
}


def _blank(v: Any) -> str:
    s = str(v or "").strip()
    return "" if s.lower() in {"unknown", "пусто", "none", "null", "n/a", "-"} else s


def _source(r: Dict[str, Any]) -> str:
    return "\n".join(_blank(r.get(k)) for k in ("район", "тип", "описание", "исходный_текст") if _blank(r.get(k))).lower()


def _numbers(v: Any, max_value: float | None = None) -> List[float]:
    vals = []
    for x in re.findall(r"\d+(?:[.,]\d+)?", _blank(v)):
        try:
            n = float(x.replace(",", "."))
        except Exception:
            continue
        if max_value is None or n <= max_value:
            vals.append(n)
    return vals


def _bedrooms(r: Dict[str, Any]) -> List[int]:
    vals = [int(x) for x in _numbers(r.get("спальни"), 20) if x >= 1 and float(x).is_integer()]
    if vals:
        return sorted(set(vals))
    txt = _source(r)
    found = []
    for p in (r"(?<!\d)(\d{1,2})\s*(?:спальн\w*|bedrooms?\b|br\b)", r"(?:спальн\w*|bedrooms?\b|br\b)\s*[:\-]?\s*(\d{1,2})(?!\d)"):
        for m in re.finditer(p, txt, re.I):
            n = int(m.group(1))
            if 1 <= n <= 20:
                found.append(n)
    return sorted(set(found))


def _prices(r: Dict[str, Any]) -> List[int]:
    vals = [int(x) for x in _numbers(r.get("цена_месяц_thb")) if int(x) >= 5000]
    if vals:
        return sorted(set(vals))
    txt = _source(r)
    found = []
    for p in (r"(?i)(\d{2,3}[\s,.]?\d{3})\s*(?:thb|бат)[^\n]{0,20}(?:/\s*мес|месяц|month)", r"(?i)(?:месяц|monthly|month)[^\n]{0,20}(\d{2,3}[\s,.]?\d{3})\s*(?:thb|бат)?"):
        for m in re.finditer(p, txt):
            n = int(re.sub(r"\D", "", m.group(1)))
            if n >= 5000:
                found.append(n)
    return sorted(set(found))


def _pool_value(c, r: Dict[str, Any]) -> str:
    p = c.norm_pool(r.get("бассейн"))
    if p in {"yes", "no"}:
        return p
    txt = _source(r)
    if re.search(r"(?i)\b(?:без\s+бассейн\w*|no\s+pool|without\s+(?:a\s+)?pool)\b", txt):
        return "no"
    if re.search(r"(?i)\b(?:бассейн\w*|pool)\b", txt):
        return "yes"
    return ""


def _type_value(c, r: Dict[str, Any]) -> str:
    typ = c.norm_type(r.get("тип"))
    if typ:
        return typ
    txt = _source(r)
    for needle, val in (("вилл", "вилла"), ("villa", "вилла"), ("бунгал", "бунгало"), ("bungal", "бунгало"), ("таунхаус", "таунхаус"), ("townhouse", "таунхаус"), ("апартамент", "апартаменты"), ("apartment", "апартаменты"), ("кондо", "кондо"), ("condo", "кондо"), ("студи", "студия"), ("studio", "студия"), ("дом", "дом"), ("house", "дом")):
        if needle in txt:
            return val
    return ""


def _district_match(c, r: Dict[str, Any], wanted: List[str]) -> Tuple[bool, bool]:
    if not wanted:
        return True, False
    raw = _blank(r.get("район"))
    if raw:
        for w in wanted:
            if c._dmatch(raw, [w]):
                return True, True
        return False, True
    txt = _source(r)
    for w in wanted:
        nw = c.norm_district(w)
        aliases = DISTRICT_ALIASES.get(nw, (nw.lower(),))
        if any(a and a in txt for a in aliases):
            return True, True
    return False, False


def _latest_active(c) -> List[Dict[str, Any]]:
    return [r for r in c._latest(c.load_catalog_rows()) if c.norm_status(r.get("status")) not in {"archived", "rented"}]


def smart_search(c, spec: Dict[str, Any], limit: int = MAX_RESULTS):
    rows = _latest_active(c)
    if spec.get("intent") == "lot":
        wanted = str(spec.get("lot_id") or "").strip()
        arr = [r for r in rows if str(r.get("lot_id") or "").strip() == wanted]
        arr.sort(key=lambda r: int(float(r.get("telegram_message_id") or 0)), reverse=True)
        return arr[:limit], False

    types = [c.norm_type(x) for x in spec.get("types", []) if c.norm_type(x)]
    allowed_types = set(types)
    if "дом" in allowed_types:
        allowed_types.update({"вилла", "бунгало", "таунхаус"})
    districts = [c.norm_district(x) for x in spec.get("districts", []) if c.norm_district(x)]
    try:
        bmin = int(spec.get("bedrooms_min")) if spec.get("bedrooms_min") is not None else None
    except Exception:
        bmin = None
    try:
        bmax = int(spec.get("bedrooms_max")) if spec.get("bedrooms_max") is not None else None
    except Exception:
        bmax = None
    max_price = c._int(spec.get("max_price_thb"))
    pool = str(spec.get("pool") or "any").lower()
    pets = str(spec.get("pets") or "any").lower()

    scored = []
    for r in rows:
        score = 0.0
        uncertain = 0
        if allowed_types:
            typ = _type_value(c, r)
            if typ:
                if typ not in allowed_types:
                    continue
                score += 5
            else:
                uncertain += 1; score -= 2
        if districts:
            ok, known = _district_match(c, r, districts)
            if known and not ok:
                continue
            if ok:
                score += 10
            else:
                uncertain += 1; score -= 5
        if bmin is not None or bmax is not None:
            beds = _bedrooms(r)
            if beds:
                good = [b for b in beds if (bmin is None or b >= bmin) and (bmax is None or b <= bmax)]
                if not good:
                    continue
                score += 9
            else:
                uncertain += 1; score -= 5
        if pool in {"yes", "no"}:
            rp = _pool_value(c, r)
            if rp:
                if rp != pool:
                    continue
                score += 9
            else:
                uncertain += 1; score -= 4
        if max_price is not None:
            prices = _prices(r)
            if prices:
                if min(prices) > max_price:
                    continue
                score += 4
            else:
                uncertain += 1; score -= 2
        if pets == "yes":
            pv = c.norm_pets(r.get("питомцы"))
            if pv == "no":
                continue
            if pv == "yes":
                score += 3
            else:
                uncertain += 1; score -= 1
        mid = c._int(r.get("telegram_message_id")) or 0
        score += min(3.0, mid / 2000.0)
        scored.append((score, -uncertain, mid, r))
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return [x[3] for x in scored[:limit]], False


def _money(v: Any) -> str:
    vals = _prices({"цена_месяц_thb": v})
    return f"{min(vals):,}".replace(",", " ") if vals else ""


def _pool_text(c, r: Dict[str, Any]) -> str:
    p = _pool_value(c, r)
    if p == "no": return "без бассейна"
    if p != "yes": return "бассейн не указан"
    t = c.norm_pool_type(r.get("тип_бассейна"))
    return {"private": "приватный бассейн", "shared": "общий бассейн", "infinity": "инфинити-бассейн"}.get(t, "есть бассейн")


def format_page(c, rows: List[Dict[str, Any]], start: int, total: int, spec: Dict[str, Any]) -> str:
    if not rows:
        return f"По каталогу @{c.CATALOG_CHANNEL} больше вариантов под текущие условия не осталось. Можно изменить район, число спален, бассейн или бюджет."
    page_no = start // PAGE_SIZE + 1
    lines = [f"🏡 Варианты из @{c.CATALOG_CHANNEL} — страница {page_no}:"]
    for i, r in enumerate(rows, start + 1):
        lot = r.get("lot_id") or r.get("telegram_message_id")
        district = _blank(r.get("район"))
        lines.append(f"{i}. Лот №{lot}" + (f" — {district}" if district else ""))
        details = []
        typ = _blank(r.get("тип")) or _type_value(c, r)
        beds = _bedrooms(r)
        if typ: details.append(typ)
        if beds: details.append("/".join(str(x) for x in beds[:3]) + " сп.")
        details.append(_pool_text(c, r))
        price = _money(r.get("цена_месяц_thb"))
        if price: details.append(f"от {price} THB/мес.")
        if details: lines.append("   " + " · ".join(details))
        url = _blank(r.get("telegram_url"))
        if url: lines.append(f"   🔗 {url}")
    shown = min(start + len(rows), total)
    if shown < total:
        lines.append(f"\nПоказал {shown} из {total}. Напишите «ещё» — покажу следующие варианты.")
    else:
        lines.append(f"\nПоказал все найденные варианты: {total}.")
    lines.append("Актуальность цены и свободных дат подтверждает менеджер.")
    return "\n".join(lines)


def _looks_property(text: str) -> bool:
    return bool(_PROPERTY_WORDS.search(text or ""))


def _is_more(text: str) -> bool:
    low = (text or "").strip().lower()
    if low in {"ещё", "еще", "ещё варианты", "еще варианты", "больше", "дальше", "следующие", "следующий"}:
        return True
    return bool(re.search(r"\b(?:и\s+вс[её]\??|больше\s+нет\??|есть\s+ещ[её]|покажи\s+ещ[её]|другие\s+варианты)\b", low))


def _explicit_type(text: str):
    low = text.lower()
    for needles, val in ((("вилла", "виллу", "villa"), "вилла"), (("дом", "дома", "house"), "дом"), (("бунгало", "bungalow"), "бунгало"), (("апартамент", "квартир", "apartment"), "апартаменты"), (("студ", "studio"), "студия"), (("кондо", "condo"), "кондо")):
        if any(n in low for n in needles): return [val]
    return None


def merge_spec(c, text: str, previous: Dict[str, Any] | None) -> Dict[str, Any]:
    raw = (text or "").strip()
    if not previous or _RESET_RE.search(raw):
        spec = c.parse_property_query(_RESET_RE.sub("", raw).strip())
        if spec.get("intent") == "other" and _looks_property(raw):
            spec = c._heuristic(raw); spec["intent"] = "search"
        return spec
    if _is_more(raw): return dict(previous)
    complete = bool(re.search(r"(?i)\b(?:дай|покажи|ищу|нужен|нужна|нужно|подбери|хочу)\b", raw))
    if complete:
        spec = c.parse_property_query(raw)
        if spec.get("intent") == "other" and _looks_property(raw):
            spec = c._heuristic(raw); spec["intent"] = "search"
        return spec
    if not _looks_property(raw): return {"intent": "other"}
    spec = dict(previous); spec["intent"] = "search"
    h = c._heuristic(raw)
    typ = _explicit_type(raw)
    if typ is not None: spec["types"] = typ
    ds = [c.norm_district(x) for x in h.get("districts", []) if c.norm_district(x)]
    if ds:
        spec["districts"] = list(dict.fromkeys(ds)); spec["district_required"] = True
    low = raw.lower()
    m = re.search(r"\bдо\s+(\d{1,2})\s*спальн", low)
    if m:
        spec["bedrooms_min"] = None; spec["bedrooms_max"] = int(m.group(1))
    else:
        m = re.search(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\s*спальн", low)
        if m:
            spec["bedrooms_min"], spec["bedrooms_max"] = int(m.group(1)), int(m.group(2))
        else:
            m = re.search(r"\b(\d{1,2})\s*спальн", low)
            if m:
                n = int(m.group(1)); spec["bedrooms_min"] = n; spec["bedrooms_max"] = n
    if "бассейн" in low or "pool" in low:
        spec["pool"] = "no" if re.search(r"без\s+бассейн|бассейн\s+не\s+нуж|no\s+pool", low) else "yes"
    if h.get("max_price_thb") is not None and re.search(r"бюджет|до\s+[\d\s]+(?:тыс|бат|thb)|не\s+дороже", low):
        spec["max_price_thb"] = h.get("max_price_thb")
    return spec


def _state(context):
    st = context.user_data.get("catalog_dialog")
    if not st: return None
    if time.time() - float(st.get("ts", 0) or 0) > STATE_TTL:
        context.user_data.pop("catalog_dialog", None); return None
    return st


def _save_state(context, spec, rows, offset):
    context.user_data["catalog_dialog"] = {"spec": spec, "rows": rows, "offset": offset, "ts": time.time()}


async def handle_text(c, legacy, update, context, fallback):
    msg = update.effective_message
    text = (getattr(msg, "text", None) or "").strip()
    if not text: return await fallback(update, context)
    st = _state(context)
    if _is_more(text) and st and st.get("spec"):
        rows = st.get("rows") or []; start = int(st.get("offset") or 0); page = rows[start:start + PAGE_SIZE]
        await msg.reply_text(format_page(c, page, start, len(rows), st["spec"]), disable_web_page_preview=True)
        st["offset"] = start + len(page); st["ts"] = time.time(); return
    prev = st.get("spec") if st else None
    spec = await asyncio.to_thread(merge_spec, c, text, prev)
    if spec.get("intent") == "lot":
        rows, relaxed = await asyncio.to_thread(smart_search, c, spec, 5)
        await msg.reply_text(c.format_catalog_answer(spec, rows, relaxed), disable_web_page_preview=True)
        _save_state(context, spec, rows, len(rows)); return
    if spec.get("intent") == "search":
        rows, _ = await asyncio.to_thread(smart_search, c, spec, MAX_RESULTS)
        if not rows:
            await msg.reply_text(f"По каталогу @{c.CATALOG_CHANNEL} вариантов под эти условия не нашёл. Можно изменить один из параметров — например район, бассейн, число спален или бюджет.", disable_web_page_preview=True)
            _save_state(context, spec, [], 0); return
        page = rows[:PAGE_SIZE]
        await msg.reply_text(format_page(c, page, 0, len(rows), spec), disable_web_page_preview=True)
        _save_state(context, spec, rows, len(page)); return
    return await fallback(update, context)


def _transcribe_sync(legacy, data: bytes, filename: str) -> str:
    if not legacy.OPENAI_API_KEY: return ""
    from openai import OpenAI
    client = OpenAI(api_key=legacy.OPENAI_API_KEY, project=legacy.OPENAI_PROJECT or None, organization=legacy.OPENAI_ORG or None, timeout=60)
    bio = io.BytesIO(data); bio.name = filename
    result = client.audio.transcriptions.create(model=TRANSCRIBE_MODEL, file=bio, language="ru")
    return (getattr(result, "text", "") or "").strip()


async def handle_voice(c, legacy, update, context, fallback):
    msg = update.effective_message
    media = getattr(msg, "voice", None) or getattr(msg, "audio", None)
    if media is None: return
    try:
        tg_file = await context.bot.get_file(media.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        ext = "ogg" if getattr(msg, "voice", None) is not None else "mp3"
        text = await asyncio.to_thread(_transcribe_sync, legacy, data, f"voice.{ext}")
        if not text:
            await msg.reply_text("Не удалось разобрать голосовое сообщение. Попробуйте ещё раз или напишите текстом."); return
        class Proxy: pass
        class MsgProxy:
            def __init__(self, original, t): self._original = original; self.text = t
            async def reply_text(self, *args, **kwargs): return await self._original.reply_text(*args, **kwargs)
        proxy = Proxy(); proxy.effective_message = MsgProxy(msg, text); proxy.message = proxy.effective_message
        proxy.effective_chat = update.effective_chat; proxy.effective_user = update.effective_user
        return await handle_text(c, legacy, proxy, context, fallback)
    except Exception:
        log.exception("Voice transcription failed")
        await msg.reply_text("Не смог обработать голосовое сообщение. Попробуйте ещё раз или напишите запрос текстом.")


def selftest(c):
    for q in ("дом 3 спальни на Ламай с бассейном", "Ламай 2 спальни", "вилла 3 спальни бассейн"):
        try:
            spec = merge_spec(c, q, None); rows, _ = smart_search(c, spec, 20)
            log.info("selftest q=%r matches=%s lots=%s", q, len(rows), [r.get("lot_id") for r in rows[:10]])
        except Exception:
            log.exception("selftest failed q=%r", q)
