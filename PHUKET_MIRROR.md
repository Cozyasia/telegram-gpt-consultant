# Phuket auto-mirror

Source: `@thailandsell`  
Destination: `@phuket_developer`

## What it does

- Watches new source posts and Telegram albums through the existing Cozy Asia MTProto client.
- Keeps source factual data unchanged: prices, areas, percentages, dates, payment schedules, ownership types, project/developer names and other numeric facts.
- Rewrites only editorial/marketing language into Cozy Asia style.
- Removes source contacts, promo blocks and source-channel links.
- Copies source media in the same order (Telegram albums are preserved in chunks of up to 10 files).
- Adds Cozy Asia CTA and Phuket hashtags.
- Stores source/destination message mapping in Google Sheet worksheet `PhuketMirror` for deduplication and audit.
- Rejects an AI rewrite when the numeric-fact set differs from the source.

## Safety model

The project already protects the stored Telethon `StringSession` from parallel connections that can cause `AUTH_KEY_DUPLICATED`.

When `PHUKET_MIRROR_ENABLED=1`:

1. one persistent MTProto client is allowed;
2. PhuketMirror attaches to that exact client;
3. `/premium_test`, `/premium_backfill` and `/mtproto_status` are blocked while mirror mode is active because they would otherwise open another client with the same authorization key;
4. if PhuketMirror cannot attach, the existing Samui bot and base MTProto process continue running.

When the environment variable is absent or `0`, the previous conservative production behaviour remains unchanged and the persistent MTProto daemon stays disabled.

## Render environment variables

Required to activate:

- `PHUKET_MIRROR_ENABLED=1`
- `PHUKET_SOURCE_CHANNEL=thailandsell`
- `PHUKET_DEST_CHANNEL=phuket_developer`
- `PHUKET_CONTACT=@Cozy_asia`

Optional:

- `PHUKET_MIRROR_MODEL` — defaults to `OPENAI_MODEL`
- `PHUKET_SOURCE_AUTOJOIN=1` — automatically joins the public source channel with the authorized MTProto account

The authorized Telegram user must have permission to post in `@phuket_developer`.

## Stories

Telegram channel Stories require two independent conditions before automation can be enabled:

- the authorized user must be an administrator with `post_stories` rights;
- the channel must have enough boosts. Telegram grants additional story capacity from channel boosts.

The post mirror should be enabled and verified first. Story publishing is intentionally kept as a separate rollout step so missing boosts or Story permissions cannot block normal channel posts.
