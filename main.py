# -*- coding: utf-8 -*-
"""Production entrypoint: legacy Telegram GPT consultant + searchable property catalog."""
import json
import logging
import os
import threading

from telegram.ext import CommandHandler, MessageHandler, filters

import legacy_main as legacy
import cozy_catalog
import catalog_fixes
import catalog_search_patch
import catalog_dialog

catalog_fixes.apply(cozy_catalog)
catalog_search_patch.apply(cozy_catalog)
# Use the full active catalog with soft fallbacks instead of a hard one-year cut-off.
cozy_catalog.search_catalog = lambda spec, limit=5: catalog_dialog.smart_search(cozy_catalog, spec, limit)

log = logging.getLogger("consultant-wrapper")
_original_free_text = legacy.free_text


def _log_google_service_account():
    raw = os.environ.get("GOOGLE_CREDS_JSON", "").strip()
    if not raw:
        return
    try:
        email = (json.loads(raw).get("client_email") or "").strip()
        if email:
            log.info("Google service account: %s", email)
    except Exception:
        log.warning("Could not parse GOOGLE_CREDS_JSON client_email")


async def catalog_aware_free_text(update, context):
    try:
        return await catalog_dialog.handle_text(
            cozy_catalog, legacy, update, context, _original_free_text
        )
    except Exception:
        log.exception("Catalog dialog failed; falling back to GPT chat")
        return await _original_free_text(update, context)


async def catalog_voice(update, context):
    return await catalog_dialog.handle_voice(
        cozy_catalog, legacy, update, context, _original_free_text
    )


def _bootstrap_catalog():
    try:
        stats = cozy_catalog.bootstrap_catalog()
        log.info("Catalog bootstrap: %s", stats)
        catalog_dialog.selftest(cozy_catalog)
    except Exception:
        log.exception("Catalog bootstrap failed")


def _install_catalog_handlers(app):
    app.add_handler(CommandHandler("catalog_import", cozy_catalog.cmd_catalog_import), group=-20)
    app.add_handler(CommandHandler("catalog_status", cozy_catalog.cmd_catalog_status), group=-20)
    app.add_handler(CommandHandler("find", cozy_catalog.cmd_find), group=-20)
    app.add_handler(CommandHandler("lot", cozy_catalog.cmd_lot), group=-20)
    app.add_handler(MessageHandler(filters.ALL, cozy_catalog.catch_catalog_updates), group=-10)
    # Voice/audio goes through OpenAI transcription and then the same stateful catalog dialog.
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, catalog_voice), group=-5)
    log.info("Catalog handlers installed for @%s", cozy_catalog.CATALOG_CHANNEL)


def main():
    legacy._log_openai_env()
    legacy._probe_openai()
    _log_google_service_account()
    legacy.free_text = catalog_aware_free_text
    app = legacy.build_application()
    _install_catalog_handlers(app)
    threading.Thread(target=_bootstrap_catalog, name="catalog-bootstrap", daemon=True).start()
    legacy.run_webhook(app)


if __name__ == "__main__":
    main()
