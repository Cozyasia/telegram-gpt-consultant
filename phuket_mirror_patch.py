# -*- coding: utf-8 -*-
"""Runtime hook that attaches Phuket mirroring to the existing MTProto client.

This keeps a single Telegram MTProto connection for the Cozy Asia process instead
of creating a second listener with the same StringSession.
"""
from __future__ import annotations

import inspect
import logging

log = logging.getLogger("phuket-mirror-patch")

_applied = False
_catalog = None


def apply():
    global _applied
    if _applied:
        return

    import mtproto_premium
    import phuket_mirror
    from telethon import TelegramClient

    original_install = mtproto_premium.install
    original_run_until_disconnected = TelegramClient.run_until_disconnected

    def patched_install(app, catalog):
        global _catalog
        _catalog = catalog
        return original_install(app, catalog)

    async def patched_run_until_disconnected(self, *args, **kwargs):
        # mtproto_premium creates the production TelegramClient. Attach the
        # Phuket source/destination handlers to that exact client before its
        # update loop starts.
        if _catalog is not None and phuket_mirror.ENABLED:
            try:
                await phuket_mirror.attach(self, _catalog)
            except Exception:
                # Never break the existing Samui/Premium daemon if the Phuket
                # channel is unavailable or permissions are not ready yet.
                log.exception("Could not attach PhuketMirror; base MTProto daemon continues")

        result = original_run_until_disconnected(self, *args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    mtproto_premium.install = patched_install
    TelegramClient.run_until_disconnected = patched_run_until_disconnected
    _applied = True
    log.info("PhuketMirror runtime hook installed")
