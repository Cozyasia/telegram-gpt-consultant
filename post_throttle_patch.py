# -*- coding: utf-8 -*-
"""Resumable flood-safe historical post migration for Cozy Asia channels."""
from __future__ import annotations

import json
import time
import post_template_patch as tpl

DONE_MARKER = "__STANDARDIZATION_DONE_V4__"
OK_PREFIX = "__STD_V4__:"
EXC_PREFIX = "__STD_V4_EXCEPTION__:"
EDIT_INTERVAL = 3.2


def _sheet_state(mod, catalog):
    processed, exceptions = set(), set()
    done = False
    try:
        vals = mod._backup_sheet(catalog).get_all_values()
        for row in vals:
            if not row:
                continue
            key = str(row[0] or "")
            if key == DONE_MARKER:
                done = True
            elif key.startswith(OK_PREFIX):
                processed.add(key[len(OK_PREFIX):])
            elif key.startswith(EXC_PREFIX):
                mid = key[len(EXC_PREFIX):]
                processed.add(mid)
                exceptions.add(mid)
    except Exception:
        mod.log.exception("Could not read V4 standardization state")
    return done, processed, exceptions


def _append_marks(mod, catalog, marks):
    if not marks:
        return
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    rows = []
    for mark in marks:
        prefix = EXC_PREFIX if mark.get("exception") else OK_PREFIX
        rows.append([
            prefix + str(mark.get("mid") or ""),
            str(mark.get("lot") or ""),
            str(mark.get("url") or ""),
            "",
            "",
            json.dumps(mark, ensure_ascii=False),
            now,
        ])
    try:
        mod._backup_sheet(catalog).append_rows(rows, value_input_option="RAW")
    except Exception:
        mod.log.exception("Could not persist V4 standardization progress")


def _mark_done(mod, catalog, stats, exception_ids):
    try:
        mod._backup_sheet(catalog).append_row([
            DONE_MARKER, "", "", "", "",
            json.dumps({"stats": stats, "exception_message_ids": exception_ids}, ensure_ascii=False),
            time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        ], value_input_option="RAW")
    except Exception:
        mod.log.exception("Could not persist V4 DONE marker")


def _raw_tg(mod, token, method, data):
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        response = mod.requests.post(url, data=data, timeout=40)
        try:
            payload = response.json()
        except Exception:
            payload = {"ok": False, "description": response.text[:500]}
        payload["_status"] = response.status_code
        return payload
    except Exception as exc:
        return {"ok": False, "description": f"network error: {exc}", "_transient": True}


def _classify(payload):
    if payload.get("ok"):
        return "ok", ""
    desc = str(payload.get("description") or "")
    low = desc.lower()
    if "message is not modified" in low:
        return "unchanged", desc
    params = payload.get("parameters") or {}
    retry = params.get("retry_after")
    if retry is not None or int(payload.get("_status") or 0) == 429:
        try:
            seconds = max(1, int(float(retry or 5)))
        except Exception:
            seconds = 5
        return "rate", str(seconds)
    if payload.get("_transient") or int(payload.get("_status") or 0) >= 500:
        return "transient", desc
    return "error", desc


def apply(mod):
    def edit_message(token, channel, mid, new_html, mode):
        common = {"chat_id": f"@{channel}", "message_id": mid, "parse_mode": "HTML"}
        primary = "editMessageCaption" if mode == "caption" else "editMessageText"
        pdata = {**common, ("caption" if mode == "caption" else "text"): new_html}
        if mode != "caption":
            pdata["disable_web_page_preview"] = "true"

        payload = _raw_tg(mod, token, primary, pdata)
        kind, detail = _classify(payload)
        if kind in {"ok", "unchanged", "rate", "transient"}:
            return kind, detail

        low = detail.lower()
        # Only use the alternate method when the public-web classification can genuinely be wrong.
        should_fallback = (
            (mode == "text" and "there is no text in the message to edit" in low)
            or (mode == "caption" and ("message is not a media message" in low or "there is no caption" in low))
        )
        if not should_fallback:
            return "error", detail

        alt_mode = "text" if mode == "caption" else "caption"
        alt_method = "editMessageText" if alt_mode == "text" else "editMessageCaption"
        adata = {**common, ("text" if alt_mode == "text" else "caption"): new_html}
        if alt_mode == "text":
            adata["disable_web_page_preview"] = "true"
        alt_payload = _raw_tg(mod, token, alt_method, adata)
        akind, adetail = _classify(alt_payload)
        return akind, adetail

    def standardize_existing(catalog):
        already_done, processed, previous_exceptions = _sheet_state(mod, catalog)
        if already_done:
            mod.log.info("V4 standardization already DONE for @%s; skipping", catalog.CATALOG_CHANNEL)
            return {"channel": catalog.CATALOG_CHANNEL, "skipped_done": True}

        token, username, can_edit, status = mod.bot_identity_and_rights(catalog.CATALOG_CHANNEL)
        mod.log.info("V4 preflight @%s bot=@%s status=%s can_edit=%s processed=%s",
                     catalog.CATALOG_CHANNEL, username, status, can_edit, len(processed))
        if not can_edit:
            raise RuntimeError(f"@{username} has no can_edit_messages in @{catalog.CATALOG_CHANNEL}")

        all_rows = [
            r for r in catalog.load_catalog_rows(True)
            if str(r.get("lot_id") or "").strip() and str(r.get("telegram_message_id") or "").strip()
        ]
        by_mid = {str(r["telegram_message_id"]): r for r in all_rows}
        pending = [r for mid, r in by_mid.items() if mid not in processed]
        pending.sort(key=lambda r: int(r.get("telegram_message_id") or 0))

        stats = {
            "channel": catalog.CATALOG_CHANNEL,
            "total": len(by_mid),
            "resumed_processed": len(processed),
            "pending": len(pending),
            "edited": 0,
            "already_standard": 0,
            "unchanged": 0,
            "exceptions": len(previous_exceptions),
        }
        if not pending:
            _mark_done(mod, catalog, stats, sorted(previous_exceptions))
            mod.log.info("V4 DONE @%s stats=%s", catalog.CATALOG_CHANNEL, stats)
            return stats

        mids = [str(r["telegram_message_id"]) for r in pending]
        links_by_mid, texts_by_mid, mode_by_mid = tpl._crawl_payloads(
            mod, catalog.CATALOG_CHANNEL, mids, catalog.MAX_PAGES
        )
        mod._backup_rows(catalog, pending, links_by_mid, texts_by_mid)

        marks_buffer = []
        exception_ids = set(previous_exceptions)
        last_api_at = 0.0

        def flush():
            nonlocal marks_buffer
            if marks_buffer:
                _append_marks(mod, catalog, marks_buffer)
                marks_buffer = []

        for index, row in enumerate(pending, 1):
            mid = str(row.get("telegram_message_id") or "")
            lot = str(row.get("lot_id") or "")
            url = str(row.get("telegram_url") or "")
            current = str(texts_by_mid.get(mid, "") or "")

            # Public Telegram pages may lag briefly, but when the signature is present no API call is needed.
            if mod.MARKER in current and "💰" in current and "Спальни:" in current:
                stats["already_standard"] += 1
                marks_buffer.append({"mid": mid, "lot": lot, "url": url, "result": "already_standard"})
            else:
                new_html = mod.build_post(row, username, links_by_mid.get(mid, []))
                attempt = 0
                while True:
                    wait = EDIT_INTERVAL - (time.monotonic() - last_api_at)
                    if wait > 0:
                        time.sleep(wait)
                    result, detail = edit_message(
                        token, catalog.CATALOG_CHANNEL, mid, new_html, mode_by_mid.get(mid, "caption")
                    )
                    last_api_at = time.monotonic()

                    if result == "rate":
                        retry_after = max(1, int(detail or 5))
                        flush()
                        mod.log.warning("V4 flood wait @%s mid=%s lot=%s retry_after=%ss",
                                        catalog.CATALOG_CHANNEL, mid, lot, retry_after)
                        time.sleep(retry_after + 2)
                        continue
                    if result == "transient" and attempt < 3:
                        attempt += 1
                        time.sleep(min(20, 2 ** attempt))
                        continue
                    if result == "ok":
                        stats["edited"] += 1
                        marks_buffer.append({"mid": mid, "lot": lot, "url": url, "result": "edited"})
                    elif result == "unchanged":
                        stats["unchanged"] += 1
                        marks_buffer.append({"mid": mid, "lot": lot, "url": url, "result": "unchanged"})
                    else:
                        stats["exceptions"] += 1
                        exception_ids.add(mid)
                        marks_buffer.append({
                            "mid": mid, "lot": lot, "url": url, "result": "exception",
                            "exception": True, "error": str(detail or "")[:500],
                        })
                        mod.log.warning("V4 exception @%s mid=%s lot=%s error=%s",
                                        catalog.CATALOG_CHANNEL, mid, lot, str(detail or "")[:420])
                    break

            if len(marks_buffer) >= 10:
                flush()
            if index % 20 == 0 or index == len(pending):
                flush()
                mod.log.info(
                    "V4 @%s %s/%s edited=%s already=%s unchanged=%s exceptions=%s",
                    catalog.CATALOG_CHANNEL, index, len(pending), stats["edited"],
                    stats["already_standard"], stats["unchanged"], stats["exceptions"],
                )

        flush()
        _mark_done(mod, catalog, stats, sorted(exception_ids))
        mod.log.info("V4 DONE @%s stats=%s exception_ids=%s",
                     catalog.CATALOG_CHANNEL, stats, sorted(exception_ids))
        return stats

    mod.standardize_existing = standardize_existing
