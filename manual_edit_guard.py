# -*- coding: utf-8 -*-
"""Protect intentional manual channel edits from automatic post standardization."""
import logging

log = logging.getLogger("manual-edit-guard")


def apply(post_standardizer):
    original = post_standardizer.standardize_future

    async def guarded(catalog, update, context):
        if getattr(update, "edited_channel_post", None) is not None:
            msg = update.edited_channel_post
            log.info(
                "Manual channel edit preserved @%s mid=%s",
                catalog.CATALOG_CHANNEL,
                getattr(msg, "message_id", "?"),
            )
            return
        return await original(catalog, update, context)

    post_standardizer.standardize_future = guarded
    log.info("Manual edit guard applied")
