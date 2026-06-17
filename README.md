# EvoLink Seedance Telegram Bot (image-to-video)

A personal Telegram bot. You tap a button, send an image, send a prompt — it
generates a Seedance 2.0 Fast video and sends it back into the chat, where it
stays permanently (EvoLink's own links die after 24h, so the bot downloads it).

Works from your iPhone. The bot code runs on a free cloud host, not your phone.

## The flow

1. Tap **🎬 New video** (or send `/go`)
2. Bot: "Send me the image" → you send a photo
3. Bot: "Now send the dialogue line" → you type just the spoken line
4. Bot drops your line into the fixed prompt, generates, and sends you the video

The full prompt (the Danish-accent / candid-phone-footage template) is stored in
the code. Only the **dialogue line** and the **image** change each time — you
never retype the prompt. If you wrap your line in quotes, the bot strips them so
they don't double up.

**Fixed every time** (baked into the code):
720p · 4 seconds · 9:16 vertical · audio on · content filter OFF (unrestricted, +10% billing).

---

## Setup (one time)

### 1. Telegram bot token
Message **@BotFather** → `/newbot` → follow prompts → copy the token.

### 2. Your Telegram ID
Message **@userinfobot** → copy the `Id:` number. (Locks the bot to just you.)

### 3. EvoLink key + credit
evolink.ai → dashboard → **API Keys** → create one. Then **Billing** → load a
small amount (~$10) to test first.

### 4. Deploy on Railway (free)
1. Put `bot.py`, `requirements.txt`, `Procfile`, `README.md` in a GitHub repo.
2. railway.app → **New Project** → **Deploy from GitHub repo** → pick it.
3. In **Variables**, add three:

   | Variable | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | token from BotFather |
   | `EVOLINK_API_KEY` | your EvoLink key |
   | `ALLOWED_USERS` | your Telegram ID |

4. **Deployments → Logs** should show `Bot starting (long-polling)…`.

Done. Open Telegram, message your bot, tap **🎬 New video**.

---

## Changing the fixed settings

Edit the `CONFIG` block at the top of `bot.py`, push to GitHub, Railway redeploys.

- `PROMPT_TEMPLATE` — the full fixed prompt. Keep `{dialogue}` where the spoken
  line should go; edit the rest only if you want to change the template itself.
- `DURATION` — 4–15 seconds
- `ASPECT_RATIO` — `"9:16"` / `"16:9"` / `"1:1"` / `"4:3"` / `"3:4"` / `"21:9"`
- `QUALITY` — `"480p"` (cheaper) or `"720p"`
- `GENERATE_AUDIO` — `True` / `False`
- `CONTENT_FILTER` — `False` = unrestricted (+10%), `True` = standard filter

## Key safety (the account is funded — this matters)

- Secrets live ONLY in Railway Variables, never in the code or a screenshot.
- The access guard fails closed: empty `ALLOWED_USERS` = bot refuses everyone.
- If the key ever leaks, regenerate it in the EvoLink dashboard; the old one dies.
- Set a low-balance alert / cap in EvoLink if available.

## Cost (image-to-video, fast)

720p ≈ ~$0.20/s → a 4-second clip ≈ **~$0.80** (plus the +10% for filter off).
480p is cheaper if you want to drop cost.

## Notes / limits

- This is **fast image-to-video**: 1 image = first-frame animation; if you ever
  send 2 images it becomes first→last-frame (the bot currently sends 1).
- Telegram caps bot uploads at 50 MB. 4s clips are well under. If one exceeds it,
  the bot sends the direct URL instead (save within 24h).
- Image rules: jpg/png/webp, aspect 0.4–2.5, 300–6000px, ≤30MB.
- Railway free tier may sleep when idle; first message after a quiet spell can
  lag a few seconds while it wakes. Normal.
- "Unrestricted" still enforces EvoLink's baseline/illegal-content moderation
  regardless of the filter setting — that part can't be turned off.
