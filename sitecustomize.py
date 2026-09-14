# -*- coding: utf-8 -*-
"""MTProto safety/bootstrap policy for Render.

Default production behaviour remains conservative: the persistent MTProto daemon
is disabled, because opening it together with short-lived premium commands using
the same Telethon StringSession can cause AUTH_KEY_DUPLICATED.

When PHUKET_MIRROR_ENABLED is explicitly enabled, we switch to one persistent
shared MTProto client. PhuketMirror is attached to that client and commands that
would open a second connection with the same auth key are blocked.
"""
from __future__ import annotations

import os


def _truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "no", "off", ""}


try:
    import mtproto_auth
    import mtproto_premium

    if _truthy("PHUKET_MIRROR_ENABLED", "0"):
        from phuket_mirror_patch import apply as _apply_phuket_mirror_patch
        from phuket_style_patch import apply as _apply_phuket_style_patch

        _apply_phuket_mirror_patch()
        _apply_phuket_style_patch()

        async def _mirror_mode_blocked(update, context, *args, **kwargs):
            msg = getattr(update, "effective_message", None)
            if msg:
                await msg.reply_text(
                    "Эта MTProto-команда временно отключена: активен постоянный PhuketMirror-клиент. "
                    "Это защита Telegram-сессии от AUTH_KEY_DUPLICATED."
                )

        # These handlers normally create a second client with the same stored
        # StringSession. Their lambdas resolve the globals at execution time, so
        # replacing the functions here keeps the one-client invariant.
        mtproto_premium.cmd_test = _mirror_mode_blocked
        mtproto_premium.cmd_backfill = _mirror_mode_blocked
        mtproto_auth.status = _mirror_mode_blocked
    else:
        # Existing safe production policy when Phuket mirroring is not enabled.
        def _disabled_mtproto_daemon(catalog):
            return None

        mtproto_premium.ensure_daemon_started = _disabled_mtproto_daemon
except Exception:
    # Never block the main bot from starting if this safety/bootstrap patch
    # cannot load (for example, during build before dependencies are installed).
    pass
