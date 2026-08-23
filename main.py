# -*- coding: utf-8 -*-
"""Production entrypoint: legacy Telegram GPT consultant + searchable property catalog."""
import asyncio
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
# Search the complete active catalog. Recency affects ranking, but no longer hides older lots.
cozy_catalog.search_catalog = lambda spec, limit=5: catalog_dialog.smart_search(cozy_catalog, spec, limit)

# Clear start message: users may either search the live catalog in free text or fill in /rent.
legacy.START_GREETING = (
    "👋 Добро пожаловать в Cozy Asia!\n\n"
    "🏡 Можете сразу написать, какое жильё ищете — я подберу варианты из нашего каталога и дам ссылки на лоты.\n"
    "Например: «Дом или вилла, Ламай / Маенам / Чавенг, 2 спальни, бассейн, до 80 000 бат».\n\n"
    "📝 Если хотите оставить подробную заявку менеджеру — нажмите /rent и ответьте на несколько вопросов.\n\n"
    "Также можете просто задавать мне вопросы о Самуи, районах, аренде и жизни на острове."
)

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
    # Voice/audio is transcribed and routed through the same stateful dialog as typed text.
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
