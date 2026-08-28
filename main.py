# -*- coding: utf-8 -*-
"""Production entrypoint: legacy Telegram GPT consultant + searchable property catalog."""
import asyncio
import json
import logging
import os
import re
import threading
import time

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

import legacy_main as legacy
import cozy_catalog
import catalog_fixes
import catalog_search_patch
import catalog_dialog
import post_standardizer
import post_template_patch
import post_throttle_patch
import post_layout_v7_safe
import manual_edit_guard
import mtproto_premium
import fix_big_channel_ctas

catalog_fixes.apply(cozy_catalog)
catalog_search_patch.apply(cozy_catalog)
cozy_catalog.search_catalog = lambda spec, limit=5: catalog_dialog.smart_search(cozy_catalog, spec, limit)
post_template_patch.apply(post_standardizer)
post_throttle_patch.apply(post_standardizer)
post_layout_v7_safe.apply(post_standardizer, post_throttle_patch)
manual_edit_guard.apply(post_standardizer)

log = logging.getLogger("consultant-wrapper")
_original_free_text = legacy.free_text

legacy.START_GREETING = (
    "👋 Добро пожаловать в Cozy Asia!\n\n"
    "🏡 Можете сразу написать, какое жильё ищете — я подберу варианты из нашего каталога и дам ссылки на лоты.\n"
    "Например: «Дом или вилла, Ламай / Маенам / Чавенг, 2 спальни, бассейн, до 80 000 бат».\n\n"
    "📝 Если хотите оставить подробную заявку менеджеру — нажмите /rent.\n"
    "🌴 Также можете просто задавать вопросы о Самуи, районах и аренде."
)

SEARCH_GREETING = (
    "🏡 Подберу варианты из каталога Cozy Asia.\n\n"
    "Напишите одним сообщением, что ищете. Например:\n"
    "«Вилла или дом, Ламай / Маенам, 2 спальни, бассейн, до 80 000 бат».\n\n"
    "Можно писать обычным текстом или отправить голосовое сообщение."
)


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


def _gpt_with_history_sync(text, history):
    from openai import OpenAI
    client = OpenAI(
        api_key=legacy.OPENAI_API_KEY,
        project=legacy.OPENAI_PROJECT or None,
        organization=legacy.OPENAI_ORG or None,
        timeout=30,
    )
    system = (
        "Ты ассистент Cozy Asia на Самуи. Отвечай по-русски, дружелюбно, кратко и по делу. "
        "Учитывай предыдущие реплики диалога и местоименные/короткие продолжения вроде "
        "'а ещё?', 'а там?', 'сколько стоит?'. Не выдумывай данные об объектах недвижимости: "
        "конкретные лоты подбирает каталог. По общим вопросам Самуи, районов и жизни отвечай нормально."
    )
    messages = [{"role": "system", "content": system}]
    for item in history[-10:]:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": text})
    resp = client.chat.completions.create(
        model=legacy.OPENAI_MODEL,
        messages=messages,
        temperature=0.5,
        max_tokens=700,
    )
    return (resp.choices[0].message.content or "").strip()


async def contextual_free_chat(update, context):
    msg = update.effective_message
    text = (getattr(msg, "text", None) or "").strip()
    if not text:
        return
    if text.lower() == "rent":
        return await _original_free_text(update, context)
    if not legacy.OPENAI_API_KEY:
        return await _original_free_text(update, context)
    history = context.user_data.get("gpt_history") or []
    try:
        answer = await asyncio.to_thread(_gpt_with_history_sync, text, history)
        if not answer:
            return await _original_free_text(update, context)
        history.extend([
            {"role": "user", "content": text},
            {"role": "assistant", "content": answer},
        ])
        context.user_data["gpt_history"] = history[-10:]
        await msg.reply_text(answer, disable_web_page_preview=True)
    except Exception:
        log.exception("Contextual GPT chat failed")
        return await _original_free_text(update, context)


async def catalog_aware_free_text(update, context):
    try:
        return await catalog_dialog.handle_text(
            cozy_catalog, legacy, update, context, contextual_free_chat
        )
    except Exception:
        log.exception("Catalog dialog failed; falling back to GPT chat")
        return await contextual_free_chat(update, context)


async def catalog_voice(update, context):
    return await catalog_dialog.handle_voice(
        cozy_catalog, legacy, update, context, contextual_free_chat
    )


async def smart_start(update, context):
    payload = "_".join(context.args or []).strip()
    low = payload.lower()

    if low == "rent" or low.startswith("rent_"):
        lot = payload[5:].strip() if low.startswith("rent_") else ""
        if lot and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", lot):
            context.user_data["lot_hint"] = lot
            log.info("Deep-link application for lot=%s", lot)
        else:
            context.user_data.pop("lot_hint", None)
            log.info("Deep-link generic application")
        return await legacy.cmd_rent(update, context)

    if low == "search":
        for key in ("catalog_spec", "catalog_rows", "catalog_offset", "catalog_ts"):
            context.user_data.pop(key, None)
        await update.effective_message.reply_text(SEARCH_GREETING)
        return ConversationHandler.END

    if payload:
        lot_match = re.fullmatch(r"(?i)(?:lot[_-]?)?([0-9]+(?:-[0-9]+)?)", payload)
        if lot_match:
            context.user_data["lot_hint"] = lot_match.group(1)
            log.info("Captured legacy start lot_hint=%s", lot_match.group(1))

    await update.effective_message.reply_text(legacy.START_GREETING)
    return ConversationHandler.END


def _bootstrap_catalog():
    try:
        stats = cozy_catalog.bootstrap_catalog()
        log.info("Catalog bootstrap: %s", stats)
        catalog_dialog.selftest(cozy_catalog)
    except Exception:
        log.exception("Catalog bootstrap failed")


def _standardize_existing():
    try:
        stats = post_standardizer.maybe_start_existing(cozy_catalog)
        if stats:
            log.info("Existing post standardization complete: %s", stats)
    except Exception:
        log.exception("Existing post standardization failed")


def _fix_big_channel_ctas_on_startup():
    time.sleep(8)
    try:
        result = asyncio.run(fix_big_channel_ctas.run())
        if result.get("enabled"):
            log.info("Targeted big-channel CTA repair complete: %s", result)
    except Exception:
        log.exception("Targeted big-channel CTA repair failed")


def _build_application():
    app = ApplicationBuilder().token(legacy.TELEGRAM_TOKEN).build()

    rent_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", smart_start),
            CommandHandler("rent", legacy.cmd_rent),
        ],
        states={
            legacy.Q_LOT: [MessageHandler(filters.TEXT & ~filters.COMMAND, legacy.q_lot)],
            legacy.Q_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, legacy.q_name)],
            legacy.Q_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, legacy.q_type)],
            legacy.Q_DISTRICT: [MessageHandler(filters.TEXT & ~filters.COMMAND, legacy.q_district)],
            legacy.Q_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, legacy.q_budget)],
            legacy.Q_BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, legacy.q_bedrooms)],
            legacy.Q_CHECKIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, legacy.q_checkin)],
            legacy.Q_CHECKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, legacy.q_checkout)],
            legacy.Q_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, legacy.q_notes)],
            legacy.Q_CONTACTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, legacy.q_contacts)],
            legacy.Q_TRANSFER: [MessageHandler(filters.TEXT & ~filters.COMMAND, legacy.q_transfer)],
        },
        fallbacks=[CommandHandler("cancel", legacy.cmd_cancel)],
        allow_reentry=True,
    )

    mtproto_premium.install(app, cozy_catalog)
    app.add_handler(CommandHandler("catalog_import", cozy_catalog.cmd_catalog_import), group=-20)
    app.add_handler(CommandHandler("catalog_status", cozy_catalog.cmd_catalog_status), group=-20)
    app.add_handler(CommandHandler("find", cozy_catalog.cmd_find), group=-20)
    app.add_handler(CommandHandler("lot", cozy_catalog.cmd_lot), group=-20)
    app.add_handler(MessageHandler(filters.ALL, cozy_catalog.catch_catalog_updates), group=-10)
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, catalog_voice), group=-5)

    app.add_handler(rent_conv)
    app.add_handler(CommandHandler("links", legacy.cmd_links))
    app.add_handler(CommandHandler("cancel", legacy.cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catalog_aware_free_text))

    post_standardizer.install(app, cozy_catalog)
    log.info("Catalog handlers installed for @%s", cozy_catalog.CATALOG_CHANNEL)
    return app


def main():
    legacy._log_openai_env()
    legacy._probe_openai()
    _log_google_service_account()
    app = _build_application()
    threading.Thread(target=_bootstrap_catalog, name="catalog-bootstrap", daemon=True).start()
    threading.Thread(target=_standardize_existing, name="post-standardizer", daemon=True).start()
    if fix_big_channel_ctas.enabled():
        threading.Thread(target=_fix_big_channel_ctas_on_startup, name="fix-big-channel-ctas", daemon=True).start()
    legacy.run_webhook(app)


if __name__ == "__main__":
    main()
