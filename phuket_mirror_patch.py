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


async def _blocked_parallel_mtproto(update, context, *args, **kwargs):
    msg = getattr(update, "effective_message", None)
    if msg:
        await msg.reply_text(
            "Эта MTProto-команда временно отключена: активен постоянный PhuketMirror-клиент. "
            "Это защита Telegram-сессии от AUTH_KEY_DUPLICATED."
        )


def apply():
    global _applied
    if _applied:
        return

    import mtproto_auth
    import mtproto_premium
    import phuket_mirror
    import phuket_story
    from telethon import TelegramClient

    original_install = mtproto_premium.install
    original_run_until_disconnected = TelegramClient.run_until_disconnected

    # Enforce the one-client invariant even if sitecustomize is not imported by
    # the runtime. These commands normally create a second client with the same
    # stored StringSession.
    mtproto_premium.cmd_test = _blocked_parallel_mtproto
    mtproto_premium.cmd_backfill = _blocked_parallel_mtproto
    mtproto_auth.status = _blocked_parallel_mtproto

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
                me = await self.get_me()
                source = await self.get_entity(phuket_mirror.SOURCE_CHANNEL)
                dest = await self.get_entity(phuket_mirror.DEST_CHANNEL)
                perms = await self.get_permissions(dest, me)
                is_creator = bool(getattr(perms, "is_creator", False))
                is_admin = bool(getattr(perms, "is_admin", False))
                post_messages = getattr(perms, "post_messages", None)
                can_post = is_creator or (is_admin and post_messages is not False)
                log.info(
                    "PhuketMirror preflight account=@%s source=@%s destination=@%s "
                    "creator=%s admin=%s post_messages=%s can_post=%s",
                    getattr(me, "username", "") or "unknown",
                    getattr(source, "username", "") or phuket_mirror.SOURCE_CHANNEL,
                    getattr(dest, "username", "") or phuket_mirror.DEST_CHANNEL,
                    is_creator,
                    is_admin,
                    post_messages,
                    can_post,
                )
                if not can_post:
                    raise RuntimeError(
                        f"Telegram account @{getattr(me, 'username', '') or 'unknown'} "
                        f"cannot post to @{phuket_mirror.DEST_CHANNEL}"
                    )
                await phuket_mirror.attach(self, _catalog)
            except Exception:
                # Never break the existing Samui/Premium daemon if the Phuket
                # channel is unavailable or permissions are not ready yet.
                log.exception("Could not attach PhuketMirror; base MTProto daemon continues")

            if phuket_story.ENABLED:
                try:
                    await phuket_story.attach(self, _catalog)
                except Exception:
                    # Stories are an optional layer. Missing boosts/story admin
                    # rights must never block normal Phuket channel posts.
                    log.exception("Could not attach PhuketStories; normal mirror continues")

        result = original_run_until_disconnected(self, *args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    mtproto_premium.install = patched_install
    TelegramClient.run_until_disconnected = patched_run_until_disconnected
    _applied = True
    log.info("PhuketMirror runtime hook installed")
