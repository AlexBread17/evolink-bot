# EvoLink Seedance Telegram Bot — 3 modes

A personal Telegram bot for Seedance 2.0 Fast image-to-video. All clips are
**480p · 4s · 9:16**, unrestricted (content filter off). Videos land in Telegram
permanently; step messages auto-clear to keep the chat tidy.

## Main menu — three buttons

**⚡ Quick Mode** — fastest. Uses your saved default image + the fixed prompt
template. You type only the dialogue line → confirm → video.

**🎛️ Flexible Mode** — you send an image, then pick:
- **💬 Dialogue box** — type only the dialogue (uses the template)
- **📝 Full prompt** — type the entire prompt yourself

**⚙️ Settings** — set or replace the Quick Mode default image, and edit the
prompt template (tap buttons, no commands):
- **🖼️ Set default image** — send a photo, becomes the Quick Mode image.
- **📝 Edit prompt template** — send new template text. It must contain
  `{dialogue}` where the spoken line goes; the bot rejects anything without it
  so the dialogue box keeps working. Applies to Quick Mode and Flexible→Dialogue.
- **👁️ View current template** — shows the active template so you can read/copy it.
- **♻️ Reset to default** — restores the built-in template.

## Video captions
- Quick Mode & Flexible→Dialogue: caption = **dialogue line + credits used**.
- Flexible→Full prompt: caption = **credits used only** (the prompt is never shown).

## Clean interface
The bot deletes its own step messages (prompts, menus, review screens, status)
as you go. The **video and its caption are never deleted**. (Telegram only lets
bots delete messages under 48h old — always true mid-session.)

## Other commands
- `/start` — show the menu.
- `/balance` — remaining EvoLink credits.
- `/cancel` — abort the current flow.

---

## Setup (one time)

1. **@BotFather** → `/newbot` → copy the token.
2. **@userinfobot** → copy your numeric ID.
3. EvoLink dashboard → create an API key, load some credit.
4. Railway → Deploy from GitHub repo → add three Variables:

   | Name | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | token from BotFather |
   | `EVOLINK_API_KEY` | your EvoLink key |
   | `ALLOWED_USERS` | your Telegram ID (comma-separated for several people) |

5. Logs should show `Bot starting (long-polling)…`. Send `/start` in Telegram.

First run: open ⚙️ Settings and set a default image so Quick Mode works.

## Editing
Edit the `CONFIG` block at the top of `bot.py` (prompt template, etc.), push to
GitHub, Railway redeploys. Secrets stay in Railway, never in the code.

## Notes
- The default image **and** your custom prompt template are stored in
  `STATE_FILE` (default `/tmp/evolink_bot_state.json`). On Railway's ephemeral
  disk these can reset on a fresh deploy — if so, just set them again in Settings.
  The built-in template in `bot.py` is the fallback whenever no custom one is saved.
- Cost at 480p ≈ ~22 credits per 4s clip (varies). Watch the caption's credit line.
- Telegram bot uploads cap at 50 MB; 4s clips are far under. If one ever exceeds
  it, the bot posts the direct link (kept, not deleted) instead.
