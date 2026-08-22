# -*- coding: utf-8 -*-
"""Final search/data-quality patch for Cozy Asia property catalog."""
import os
import re
from datetime import datetime, timedelta, timezone


def apply(c):
    orig_extract = c.extract_lot_id
    orig_parse = c.parse_property_query
    orig_canonical = c.canonical
    orig_norm_district = c.norm_district
    orig_blank = c._blank

    def blank(v):
        s = str(v or "").strip()
        if s.lower() in {"пусто", "unknown", "none", "null"}:
            return ""
        return orig_blank(v)

    aliases = {
        "ламаи": "Ламай", "ламая": "Ламай", "lamai": "Ламай",
        "бо пхут": "Бопхут", "bophut": "Бопхут", "bo phut": "Бопхут",
        "банг рак": "Банграк", "банкрак": "Банграк", "bangrak": "Банграк", "bang rak": "Банграк",
        "май нам": "Маенам", "мае нам": "Маенам", "maenam": "Маенам", "mae nam": "Маенам",
        "плайлаем": "Плай Лаем", "плай лаэм": "Плай Лаем", "plai laem": "Плай Лаем",
        "липа-ной": "Липа Ной", "липа ной": "Липа Ной", "lipa noi": "Липа Ной",
        "талинг нам": "Талинг Нгам", "талинг нгам": "Талинг Нгам", "taling ngam": "Талинг Нгам",
        "чавенг ной": "Чавенг Ной", "chaweng noi": "Чавенг Ной",
        "чавенг": "Чавенг", "chaweng": "Чавенг",
        "натон": "Натон", "nathon": "Натон", "naton": "Натон",
        "на-муанг": "На Муанг", "на муанг": "На Муанг", "namuang": "На Муанг", "na muang": "На Муанг",
        "банг по": "Банг По", "bang po": "Банг По", "bong por": "Банг По",
        "чонгмон": "Чонг Мон", "чонг мон": "Чонг Мон", "choeng mon": "Чонг Мон",
        "бан тай": "Бан Тай", "bantai": "Бан Тай", "ban tai": "Бан Тай",
        "хуа танон": "Хуа Танон", "hua thanon": "Хуа Танон", "hua tanon": "Хуа Танон",
        "марет": "Ламай", "maret": "Ламай",
    }

    def norm_district(v):
        s = blank(v)
        if not s:
            return ""
        low = re.sub(r"\s+", " ", s.lower().replace("_", " ")).strip()
        if low in {"cozy asia", "тихий", "тихий район", "koh samui", "самуи"}:
            return ""
        if low in aliases:
            return aliases[low]
        return orig_norm_district(s)

    def dmatch(rd, wanted):
        rd = norm_district(rd)
        if not rd:
            return False
        for item in wanted:
            w = norm_district(item)
            if w and (w in rd or rd in w):
                return True
        return False

    def clean_lot(token):
        token = re.sub(r"\s+", "", str(token or "")).strip("-")
        if not token:
            return ""
        if "-" in token:
            return token
        return token.lstrip("0") or "0"

    def extract_lot_id(text):
        norm = c._digits(text or "")
        lines = [x.strip() for x in norm.splitlines()[:40] if x.strip()]
        head = "\n".join(lines[:20])
        m = re.search(r"(?i)(?:лот|lot)\s*(?:№|#|no\.?)?\s*[:\-]?\s*(\d{1,7}(?:\s*-\s*\d{1,7})?)", head)
        if m:
            return clean_lot(m.group(1))
        for i, line in enumerate(lines[:20]):
            if line != "-":
                continue
            before = []
            j = i - 1
            while j >= 0 and re.fullmatch(r"\d", lines[j]):
                before.append(lines[j]); j -= 1
            before.reverse()
            after = []
            j = i + 1
            while j < len(lines) and re.fullmatch(r"\d", lines[j]):
                after.append(lines[j]); j += 1
            left, right = "".join(before), "".join(after)
            if left == "01" and len(right) >= 3:
                token = f"01-{right}"
                if j < len(lines) and lines[j] == "-":
                    k, tail = j + 1, []
                    while k < len(lines) and re.fullmatch(r"\d", lines[k]):
                        tail.append(lines[k]); k += 1
                    if tail:
                        token += "-" + "".join(tail)
                return token
            if len(left) >= 3 and 1 <= len(right) <= 3:
                return f"{left.lstrip('0') or '0'}-{right.lstrip('0') or '0'}"
        compact = re.sub(r"\s+", "", head)
        m = re.search(r"(?<!\d)(01-\d{3,7}(?:-\d{1,3})?|\d{3,7}-\d{1,3})(?!\d)", compact)
        if m:
            return clean_lot(m.group(1))
        return orig_extract(text)

    def canonical(rec, source=""):
        out = orig_canonical(rec, source)
        if source:
            lot = extract_lot_id(source)
            if lot:
                out["lot_id"] = lot
        out["район"] = norm_district(out.get("район", ""))
        for k in ("тип", "спальни", "ванные", "бассейн", "тип_бассейна", "цена_месяц_thb", "цена_сутки_thb", "депозит", "комиссия", "до_моря_м", "доступность", "питомцы", "электричество", "вода"):
            if str(out.get(k, "")).strip().lower() == "пусто":
                out[k] = ""
        return out

    district_tokens = [
        ("Ламай", ("ламай", "lamai", "maret", "марет")),
        ("Бопхут", ("бопхут", "бо пхут", "bophut", "bo phut")),
        ("Банграк", ("банграк", "банг рак", "bangrak", "bang rak")),
        ("Маенам", ("маенам", "май нам", "maenam", "mae nam")),
        ("Плай Лаем", ("плай ла", "plai laem")),
        ("Липа Ной", ("липа ной", "липа-ной", "lipa noi")),
        ("Талинг Нгам", ("талинг нгам", "талинг нам", "taling ngam")),
        ("Чавенг Ной", ("чавенг ной", "chaweng noi")),
        ("Чавенг", ("чавенг", "chaweng")),
        ("Натон", ("натон", "nathon", "naton")),
        ("На Муанг", ("на муанг", "на-муанг", "namuang", "na muang")),
        ("Банг По", ("банг по", "bang po")),
        ("Чонг Мон", ("чонг мон", "чонгмон", "choeng mon")),
        ("Бан Тай", ("бан тай", "bantai", "ban tai")),
        ("Хуа Танон", ("хуа танон", "hua thanon", "hua tanon")),
    ]

    def compact_query(text):
        low = (text or "").lower().replace("ё", "е")
        signal = any(x in low for x in ("спальн", "бассейн", "бат", "thb", "ламай", "бопхут", "чавенг", "маенам", "банграк", "плай", "липа", "натон", "вилла", "дом", "студи", "апартамент", "кондо", "бунгало"))
        if not signal:
            return None
        s = {"intent": "search", "types": [], "districts": [], "district_required": True, "bedrooms_min": None, "bedrooms_max": None, "pool": "any", "max_price_thb": None, "max_distance_sea_m": None, "pets": "any"}
        for name, toks in district_tokens:
            if any(t in low for t in toks):
                s["districts"].append(name)
        if "вилла" in low:
            s["types"] = ["вилла"]
        elif re.search(r"\bдом\b", low):
            s["types"] = ["дом"]
        elif "бунгало" in low:
            s["types"] = ["бунгало"]
        elif "студи" in low:
            s["types"] = ["студия"]
        elif "кондо" in low:
            s["types"] = ["кондо"]
        elif "апартамент" in low or "квартир" in low:
            s["types"] = ["апартаменты"]
        words = {"одна":1,"один":1,"две":2,"два":2,"три":3,"четыре":4,"пять":5,"шесть":6}
        m = re.search(r"\b(\d+|одна|один|две|два|три|четыре|пять|шесть)\s*(?:спальн|br\b)", low)
        if m:
            n = int(m.group(1)) if m.group(1).isdigit() else words[m.group(1)]
            s["bedrooms_min"] = s["bedrooms_max"] = n
        if "бассейн" in low or "pool" in low:
            s["pool"] = "no" if re.search(r"без\s+бассейн|бассейн\s+не\s+нуж", low) else "yes"
        m = re.search(r"(?:до|бюджет(?:ом)?|не\s+дороже)\s*([\d\s'’.,]+)\s*(тыс(?:яч)?|k)?\s*(?:бат|thb)?", low)
        if m:
            digs = re.sub(r"\D", "", m.group(1))
            if digs:
                n = int(digs); s["max_price_thb"] = n * 1000 if m.group(2) else n
        m = re.search(r"(?:до|не\s+дальше)\s*(\d{2,5})\s*(?:м\b|метр)", low)
        if m:
            s["max_distance_sea_m"] = int(m.group(1))
        if any(x in low for x in ("с собак", "с кош", "с живот", "питом")):
            s["pets"] = "yes"
        return s

    def parse_query(text):
        raw = text or ""
        m = re.search(r"(?i)\b(?:лот|lot)\s*(?:№|#)?\s*(\d{1,7}(?:\s*-\s*\d{1,7})?)\b", raw)
        if m:
            return {"intent": "lot", "lot_id": clean_lot(m.group(1))}
        base = orig_parse(text)
        if base.get("intent") in {"search", "lot"}:
            return base
        return compact_query(text) or base

    def dt(v):
        s = blank(v)
        if not s:
            return None
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc)
        except Exception:
            return None

    def fresh(rows):
        try:
            days = int(os.getenv("CATALOG_MAX_AGE_DAYS", "365") or 365)
        except Exception:
            days = 365
        if days <= 0:
            return list(rows)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        recent_mids = []
        for r in rows:
            d = dt(r.get("published_at")); mid = c._int(r.get("telegram_message_id"))
            if d and d >= cutoff and mid is not None:
                recent_mids.append(mid)
        fallback = min(recent_mids) if recent_mids else None
        out = []
        for r in rows:
            d = dt(r.get("published_at"))
            if d:
                if d >= cutoff:
                    out.append(r)
            elif fallback is not None:
                mid = c._int(r.get("telegram_message_id"))
                if mid is not None and mid >= fallback:
                    out.append(r)
        return out

    def nums(v, ceiling=None):
        out = []
        for x in re.findall(r"\d+(?:[.,]\d+)?", blank(v)):
            try:
                n = float(x.replace(",", "."))
            except Exception:
                continue
            if n <= 0 or (ceiling is not None and n > ceiling):
                continue
            out.append(int(n) if n.is_integer() else n)
        return out

    def prices(v):
        return [int(x) for x in nums(v) if x >= 1000]

    def search_catalog(spec, limit=5):
        all_rows = [r for r in c._latest(c.load_catalog_rows()) if c.norm_status(r.get("status")) not in {"archived", "rented"}]
        if spec.get("intent") == "lot":
            wanted = str(spec.get("lot_id") or "").strip()
            arr = [r for r in all_rows if str(r.get("lot_id") or "").strip() == wanted]
            arr.sort(key=lambda r: int(r.get("telegram_message_id") or 0), reverse=True)
            return arr[:limit], False
        rows = fresh(all_rows)
        wanted_types = {c.norm_type(x) for x in spec.get("types", []) if c.norm_type(x)}
        if "дом" in wanted_types:
            wanted_types.update({"вилла", "бунгало", "таунхаус"})
        wanted_districts = [norm_district(x) for x in spec.get("districts", []) if norm_district(x)]
        try: bmin = int(spec["bedrooms_min"]) if spec.get("bedrooms_min") is not None else None
        except Exception: bmin = None
        try: bmax = int(spec["bedrooms_max"]) if spec.get("bedrooms_max") is not None else None
        except Exception: bmax = None
        max_price = c._int(spec.get("max_price_thb")); max_dist = c._int(spec.get("max_distance_sea_m"))
        pool = str(spec.get("pool") or "any"); pets = str(spec.get("pets") or "any")
        district_required = bool(spec.get("district_required", True))

        def score(r, relax=False):
            sc = 0.0; typ = c.norm_type(r.get("тип")); beds = nums(r.get("спальни"), ceiling=20)
            pvals = prices(r.get("цена_месяц_thb")); price = min(pvals) if pvals else None
            dist = c._int(r.get("до_моря_м")); row_pool = c.norm_pool(r.get("бассейн")); row_pets = c.norm_pets(r.get("питомцы"))
            if wanted_types:
                if typ not in wanted_types: return None
                sc += 4
            if wanted_districts:
                ok = dmatch(r.get("район", ""), wanted_districts)
                if district_required and not relax and not ok: return None
                sc += 5 if ok else -2
            if bmin is not None or bmax is not None:
                if not beds: return None
                if not any((bmin is None or b >= bmin) and (bmax is None or b <= bmax) for b in beds): return None
                sc += 4
            if pool == "yes" and row_pool != "yes": return None
            if pool == "no" and row_pool != "no": return None
            if pool != "any": sc += 5
            if max_price is not None and (price is None or price > max_price): return None
            if max_price is not None: sc += 2
            if max_dist is not None and (dist is None or dist > max_dist): return None
            if max_dist is not None: sc += 3
            if pets == "yes" and row_pets != "yes": return None
            if pets == "yes": sc += 3
            return sc + min(2, int(r.get("telegram_message_id") or 0) / 100000)

        arr = [(score(r), r) for r in rows]; arr = [x for x in arr if x[0] is not None]; relaxed = False
        if not arr and wanted_districts:
            relaxed = True; arr = [(score(r, True), r) for r in rows]; arr = [x for x in arr if x[0] is not None]
        arr.sort(key=lambda x: (x[0], int(x[1].get("telegram_message_id") or 0)), reverse=True)
        return [r for _, r in arr[:limit]], relaxed

    c._blank = blank
    c.norm_district = norm_district
    c._dmatch = dmatch
    c.extract_lot_id = extract_lot_id
    c.canonical = canonical
    c.parse_property_query = parse_query
    c.search_catalog = search_catalog
