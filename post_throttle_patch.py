# -*- coding: utf-8 -*-
"""Final historical migration: serialize channel edits and skip already standardized posts."""
import time
import json
import post_template_patch as tpl

DONE_MARKER = "__STANDARDIZATION_DONE_V3__"
EDIT_INTERVAL = 1.15


def _already_done(mod, catalog):
    try:
        vals = mod._backup_sheet(catalog).get_all_values()
        return any(r and r[0] == DONE_MARKER for r in vals)
    except Exception:
        return False


def _mark_done(mod, catalog, stats, failed_ids):
    try:
        mod._backup_sheet(catalog).append_row([
            DONE_MARKER, "", "", "", "",
            json.dumps({"stats": stats, "failed_message_ids": failed_ids}, ensure_ascii=False),
            time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        ], value_input_option="RAW")
    except Exception:
        mod.log.exception("Could not write V3 standardization marker")


def apply(mod):
    last_edit = [0.0]

    def api_edit(token, method, data):
        wait = EDIT_INTERVAL - (time.monotonic() - last_edit[0])
        if wait > 0:
            time.sleep(wait)
        result = mod._tg(token, method, data, tries=8)
        last_edit[0] = time.monotonic()
        return result

    def edit_exact(token, channel, mid, new_html, mode):
        common = {"chat_id": f"@{channel}", "message_id": mid, "parse_mode": "HTML"}
        order = ["caption", "text"] if mode == "caption" else ["text", "caption"]
        errors = []
        for kind in order:
            if kind == "caption":
                payload = api_edit(token, "editMessageCaption", {**common, "caption": new_html})
            else:
                payload = api_edit(token, "editMessageText", {**common, "text": new_html, "disable_web_page_preview": "true"})
            if payload.get("ok"):
                return "edited_" + kind, ""
            desc = str(payload.get("description") or "")
            if "message is not modified" in desc.lower():
                return "unchanged", ""
            errors.append(kind + ": " + desc)
        return "failed", " | ".join(errors)

    def standardize_existing(catalog):
        if _already_done(mod, catalog):
            mod.log.info("V3 standardization already DONE for @%s; skipping", catalog.CATALOG_CHANNEL)
            return {"channel": catalog.CATALOG_CHANNEL, "skipped_done": True}

        token, username, can_edit, status = mod.bot_identity_and_rights(catalog.CATALOG_CHANNEL)
        mod.log.info("V3 preflight @%s bot=@%s status=%s can_edit=%s", catalog.CATALOG_CHANNEL, username, status, can_edit)
        if not can_edit:
            raise RuntimeError(f"@{username} has no can_edit_messages in @{catalog.CATALOG_CHANNEL}")

        rows = [r for r in catalog.load_catalog_rows(True)
                if str(r.get("lot_id") or "").strip() and str(r.get("telegram_message_id") or "").strip()]
        by_mid = {str(r["telegram_message_id"]): r for r in rows}
        rows = list(by_mid.values())
        mids = list(by_mid)
        links_by_mid, texts_by_mid, mode_by_mid = tpl._crawl_payloads(mod, catalog.CATALOG_CHANNEL, mids, catalog.MAX_PAGES)
        mod._backup_rows(catalog, rows, links_by_mid, texts_by_mid)

        stats = {"channel": catalog.CATALOG_CHANNEL, "total": len(rows), "edited": 0,
                 "already_standard": 0, "unchanged": 0, "failed": 0}
        failed_ids = []
        ordered = sorted(rows, key=lambda x: int(x.get("telegram_message_id") or 0))

        for idx, row in enumerate(ordered, 1):
            mid = str(row["telegram_message_id"])
            current = texts_by_mid.get(mid, "") or ""
            # Rich V2/V3 visible signature. No Telegram call is needed for these posts.
            if mod.MARKER in current and "· 🏠" in current and "💰" in current:
                stats["already_standard"] += 1
            else:
                new_html = mod.build_post(row, username, links_by_mid.get(mid, []))
                result, err = edit_exact(token, catalog.CATALOG_CHANNEL, mid, new_html, mode_by_mid.get(mid, "caption"))
                if result.startswith("edited"):
                    stats["edited"] += 1
                elif result == "unchanged":
                    stats["unchanged"] += 1
                else:
                    stats["failed"] += 1
                    failed_ids.append(mid)
                    mod.log.warning("V3 failed @%s mid=%s lot=%s error=%s", catalog.CATALOG_CHANNEL, mid, row.get("lot_id"), (err or "")[:420])
            if idx % 20 == 0 or idx == len(ordered):
                mod.log.info("V3 @%s %s/%s edited=%s already=%s unchanged=%s failed=%s",
                             catalog.CATALOG_CHANNEL, idx, len(ordered), stats["edited"], stats["already_standard"], stats["unchanged"], stats["failed"])

        _mark_done(mod, catalog, stats, failed_ids)
        mod.log.info("V3 DONE @%s stats=%s failed_ids=%s", catalog.CATALOG_CHANNEL, stats, failed_ids)
        return stats

    mod.standardize_existing = standardize_existing
