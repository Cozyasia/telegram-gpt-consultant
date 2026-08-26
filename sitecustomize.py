# -*- coding: utf-8 -*-
"""Safety guard for MTProto session reuse on Render.

Telethon StringSession contains a single Telegram authorization key. Running the
persistent premium daemon and /premium_test or /premium_backfill at the same time
can open parallel main-DC connections with the same auth key. Telegram may
invalidate that key (AUTH_KEY_DUPLICATED). Until the premium worker is refactored
to use one shared client/queue, keep the persistent daemon disabled. Manual
premium_test/backfill continue to work with short-lived clients.
"""
try:
    import mtproto_premium

    def _disabled_mtproto_daemon(catalog):
        return None

    mtproto_premium.ensure_daemon_started = _disabled_mtproto_daemon
except Exception:
    # Never block the main bot from starting if this safety patch cannot load.
    pass
