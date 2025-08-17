# CozyAsia Telegram GPT Consultant Bot

A minimal, production-ready Telegram bot that uses **python-telegram-bot v21** and the **OpenAI Python SDK** to answer user questions as a real-estate consultant for Samui. It runs with **long polling**, perfect for Render *Background Worker* or any VM/host without inbound ports.

---

## 1) Quick Start (local)

1. **Python 3.11+** recommended.
2. Copy env template and fill secrets:
   ```bash
   cp .env.example .env
   # put your tokens in .env
   ```
3. Install deps:
   ```bash
   pip install -r requirements.txt
   ```
4. Run the bot (polling):
   ```bash
   python main.py
   ```

The bot logs will show `Bot started (polling)...`

---

## 2) Connect the bot to your channel

1. Open your channel **@samuirental** → *Administrators* → **Add Admin** → choose your bot `@Cozy_Asia_bot` → give it permission to post messages.
2. (Optional for groups) In **BotFather**: disable *Privacy Mode* so the bot can read all group messages.

> For channels, your bot will receive posts as `channel_post` updates.

---

## 3) Deploy on Render (Background Worker)

1. Create a **Background Worker** service.
2. **Build Command**: `pip install -r requirements.txt`
3. **Start Command**: `python main.py`
4. Add environment variables:
   - `TELEGRAM_BOT_TOKEN`: from BotFather
   - `OPENAI_API_KEY`: from OpenAI
   - `OPENAI_MODEL`: (optional) default `gpt-4o-mini`
   - `SYSTEM_PROMPT`: (optional) custom role instructions

Polling requires no public URL and works well for workers.

---

## 4) Files

- `main.py` — bot logic (async, PTB v21).
- `requirements.txt` — dependencies.
- `.env.example` — env template.
- `system_prompt.txt` — default system prompt for GPT (editable).
- `render.yaml` — optional Render config (worker).

---

## 5) Commands

- `/start` — greet & short info.
- Any text in private chat or channel post → forwarded to GPT with the CozyAsia system prompt and answered back.

---

## 6) Troubleshooting

- **No replies in channel**: Make sure the bot is an **admin** of the channel and has permission to post. Confirm you're handling `channel_post` updates.
- **No replies anywhere**: Check your token in `.env`, network egress to api.telegram.org and api.openai.com, and logs.
- **OpenAI errors**: Usually token/quota or malformed request. Logs will show details.

---

## 7) Security Notes

- Keep `.env` private; never commit it.
- Consider adding basic rate-limiting or a whitelist for production.
