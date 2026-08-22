# -*- coding: utf-8 -*-
"""Small data-quality fixes layered over cozy_catalog without changing its public API."""
import re


def apply(c):
    original_extract_lot_id = c.extract_lot_id
    original_canonical = c.canonical
    original_parse_query = c.parse_property_query

    def extract_lot_id(text):
        norm = c._digits(text or "")
        head = "\n".join([x.strip() for x in norm.splitlines()[:30] if x.strip()][:15])
        m = re.search(r"(?<!\d)0*1\s*-\s*0*(\d{1,7})(?:\s*-\s*0*(\d{1,3}))?(?!\d)", head)
        if m:
            base = m.group(1).lstrip("0") or "0"
            suffix = (m.group(2) or "").lstrip("0")
            return f"{base}-{suffix}" if suffix else base
        return original_extract_lot_id(text)

    def explicit_bathrooms(source):
        t = source or ""
        patterns = [
            r"(?i)(\d+(?:[.,]\d+)?)\s*(?:ванн(?:ая|ые|ых|ой|ую|ыми)?\s*(?:комнат(?:а|ы|ах|ы)?|комнат)?|санузл(?:а|ов|ы)?|bathrooms?\b)",
            r"(?i)(?:ванн(?:ая|ые|ых|ой|ую)?\s*(?:комнат(?:а|ы|ах)?|комнат)?|санузл(?:а|ов|ы)?|bathrooms?\b)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)",
        ]
        for p in patterns:
            m = re.search(p, t)
            if m:
                return m.group(1).replace(".", ",")
        return ""

    def source_pool_type(source):
        low = (source or "").lower()
        if re.search(r"(?:инфинити|infinity)[-\s]*(?:бассейн|pool)|(?:бассейн|pool).{0,25}(?:инфинити|infinity)", low, re.S):
            return "infinity"
        if re.search(r"(?:приватн|частн|собственн).{0,25}(?:бассейн|pool)|(?:бассейн|pool).{0,25}(?:приватн|частн|собственн|private)", low, re.S):
            return "private"
        if re.search(r"(?:общ(?:ий|его)|shared|communal).{0,25}(?:бассейн|pool)|(?:бассейн|pool).{0,25}(?:общ(?:ий|его)|shared|communal)", low, re.S):
            return "shared"
        return ""

    def canonical(rec, source=""):
        out = original_canonical(rec, source)
        if source:
            lot = extract_lot_id(source)
            if lot:
                out["lot_id"] = lot
            baths = explicit_bathrooms(source)
            current = str(out.get("ванные") or "").replace(" ", "")
            try:
                bad = bool(current) and float(current.replace(",", ".")) > 30
            except Exception:
                bad = False
            if baths and (bad or not current or "," in baths):
                out["ванные"] = baths
            pt = source_pool_type(source)
            if pt:
                out["тип_бассейна"] = pt
                out["бассейн"] = "yes"
            low = str(out.get("район") or "").lower().strip()
            aliases = {
                "huathanon": "Хуа Танон", "hua tanon": "Хуа Танон", "naton": "Натон",
                "maret": "Ламай", "марет": "Ламай", "bang-po": "Банг По", "bang po": "Банг По",
                "na-muang": "На Муанг", "namuang": "На Муанг", "на-муанг": "На Муанг",
            }
            if low in aliases:
                out["район"] = aliases[low]
        return out

    def parse_property_query(text):
        m = re.search(r"(?i)\b(?:лот|lot)\s*(?:№|#)?\s*(\d{1,7}(?:\s*-\s*\d{1,3})?)\b", text or "")
        if m:
            return {"intent": "lot", "lot_id": re.sub(r"\s+", "", m.group(1))}
        return original_parse_query(text)

    c.extract_lot_id = extract_lot_id
    c.canonical = canonical
    c.parse_property_query = parse_property_query
