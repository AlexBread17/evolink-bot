"""
EvoLink Seedance 2.0 Fast (image-to-video) Telegram bot.

Flow per video:
  1. You tap the "New video" button (or send /go)
  2. Bot asks for an image     -> you send a photo
  3. Bot asks for the dialogue -> you type just the spoken line
  4. Bot drops your line into the fixed prompt template, uploads the image,
     generates, and sends the video back

The prompt is a fixed template (see PROMPT_TEMPLATE); only the dialogue line and
the image change each time. Duration, aspect ratio, quality and the content
filter are also fixed in CONFIG. The finished video stays in Telegram permanently
— EvoLink's own links expire in 24h, so the bot downloads and sends it.
"""

import os
import asyncio
import logging
import aiohttp

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# CONFIG  -- the part you actually touch
# ---------------------------------------------------------------------------
# Secrets come from the host's environment variables. Never put them in this file.
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
EVOLINK_KEY = os.environ["EVOLINK_API_KEY"]
ALLOWED_USERS = {
    int(uid) for uid in os.environ.get("ALLOWED_USERS", "").split(",") if uid.strip()
}

# Fixed generation settings (same every time, per your spec).
MODEL = "seedance-2.0-fast-image-to-video"
DURATION = 4              # seconds
ASPECT_RATIO = "9:16"     # vertical
QUALITY = "720p"          # "480p" or "720p" (fast i2v doesn't offer 1080p yet)
GENERATE_AUDIO = True
CONTENT_FILTER = False    # unrestricted / relaxed mode (+10% billing)

# Fixed prompt template. {dialogue} is replaced by the line you send each time;
# everything else stays exactly as-is. Edit this block only if you ever want to
# change the template itself.
PROMPT_TEMPLATE = (
    "She moves with relaxed but lively energy — walking around naturally so the "
    "background shifts behind her, with easy reframing, gentle tilts and turns as "
    "she goes. Casual handheld movement with natural micro-shake, selfie style with "
    "her arm extended. Her face stays flat and emotionless throughout: neutral "
    "expression, no smiling, minimal eyebrow movement. She speaks just above a "
    "whisper with a Danish accent — quiet and breathy, low vocal effort, words still "
    "clearly audible. Flat, deadpan delivery. She ends the sentence on a flat, "
    "falling pitch — not rising or upbeat — letting the final word settle naturally, "
    "followed by about half a second of silence before the clip ends. "
    "Dialogue: \u201c{dialogue}\u201d "
    "Hyper-realistic, candid amateur phone footage, authentic and unpolished."
)

# Endpoints (confirmed from EvoLink docs)
GEN_URL = "https://api.evolink.ai/v1/videos/generations"
TASK_URL = "https://api.evolink.ai/v1/tasks/{task_id}"
UPLOAD_URL = "https://files-api.evolink.ai/api/v1/files/upload/stream"

# Polling
POLL_INTERVAL = 5
POLL_TIMEOUT = 600        # 10 min

# Telegram bot-upload ceiling
TG_UPLOAD_LIMIT = 50 * 1024 * 1024

# Conversation states
ASK_IMAGE, ASK_DIALOGUE = range(2)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("evolink-bot")

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["🎬 New video"]], resize_keyboard=True, one_time_keyboard=False
)


def authorised(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and user.id in ALLOWED_USERS


# ---------------------------------------------------------------------------
# EvoLink calls
# ---------------------------------------------------------------------------
async def upload_image(session: aiohttp.ClientSession, img_bytes: bytes,
                       filename: str = "frame.jpg") -> str:
    """Upload the photo to EvoLink's file service, return its public file_url."""
    form = aiohttp.FormData()
    form.add_field("file", img_bytes, filename=filename,
                   content_type="image/jpeg")
    headers = {"Authorization": f"Bearer {EVOLINK_KEY}"}
    async with session.post(UPLOAD_URL, data=form, headers=headers) as r:
        body = await r.json()
        if r.status >= 400 or not body.get("success", True):
            raise RuntimeError(f"Image upload failed ({r.status}): {body}")
        url = (body.get("data") or {}).get("file_url")
        if not url:
            raise RuntimeError(f"No file_url in upload response: {body}")
        return url


async def submit_job(session: aiohttp.ClientSession, prompt: str,
                     image_url: str) -> str:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "image_urls": [image_url],
        "duration": DURATION,
        "quality": QUALITY,
        "aspect_ratio": ASPECT_RATIO,
        "generate_audio": GENERATE_AUDIO,
        "content_filter": CONTENT_FILTER,
    }
    headers = {"Authorization": f"Bearer {EVOLINK_KEY}"}
    async with session.post(GEN_URL, json=payload, headers=headers) as r:
        body = await r.json()
        if r.status == 401:
            raise RuntimeError("EvoLink rejected the key (401). Check it in the dashboard.")
        if r.status == 402:
            raise RuntimeError("EvoLink balance too low (402). Top up credits.")
        if r.status >= 400:
            raise RuntimeError(f"Submit error {r.status}: {body}")
        task_id = body.get("id")
        if not task_id:
            raise RuntimeError(f"No task id returned: {body}")
        return task_id


async def poll_job(session: aiohttp.ClientSession, task_id: str) -> str:
    """Poll until completed; return the video URL from results[0]."""
    headers = {"Authorization": f"Bearer {EVOLINK_KEY}"}
    url = TASK_URL.format(task_id=task_id)
    waited = 0
    while waited < POLL_TIMEOUT:
        async with session.get(url, headers=headers) as r:
            body = await r.json()
            status = body.get("status")
            if status == "completed":
                results = body.get("results") or []
                if results:
                    return results[0]
                raise RuntimeError(f"Completed but no results: {body}")
            if status == "failed":
                err = (body.get("error") or {}).get("message", "unknown error")
                raise RuntimeError(f"Generation failed: {err}")
        await asyncio.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
    raise TimeoutError(f"Timed out after {POLL_TIMEOUT}s. Task id: {task_id}")


async def fetch_bytes(session: aiohttp.ClientSession, url: str):
    async with session.get(url) as r:
        r.raise_for_status()
        size = int(r.headers.get("Content-Length", 0))
        if size and size > TG_UPLOAD_LIMIT:
            return None, size
        data = await r.read()
        if len(data) > TG_UPLOAD_LIMIT:
            return None, len(data)
        return data, len(data)


# ---------------------------------------------------------------------------
# Conversation handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    await update.message.reply_text(
        "Ready. Tap “🎬 New video” (or send /go) to make one.\n"
        f"Fixed settings: {QUALITY}, {DURATION}s, {ASPECT_RATIO}, "
        f"audio {'on' if GENERATE_AUDIO else 'off'}, "
        f"filter {'off' if not CONTENT_FILTER else 'on'}.",
        reply_markup=MAIN_KEYBOARD,
    )


async def go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    await update.message.reply_text("Send me the image (as a photo).")
    return ASK_IMAGE


async def got_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END

    # Accept either a compressed photo or an image sent as a file/document.
    file_id = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id          # highest resolution
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        file_id = update.message.document.file_id

    if not file_id:
        await update.message.reply_text("That wasn't an image. Send a photo to continue.")
        return ASK_IMAGE

    tg_file = await context.bot.get_file(file_id)
    img_bytes = bytes(await tg_file.download_as_bytearray())
    context.user_data["image_bytes"] = img_bytes

    await update.message.reply_text("Got it. Now send the dialogue line.")
    return ASK_DIALOGUE


async def got_dialogue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END

    dialogue = (update.message.text or "").strip()
    if not dialogue:
        await update.message.reply_text("Send the dialogue line as text.")
        return ASK_DIALOGUE

    # Strip any quotes the user typed around it, since the template adds its own.
    dialogue = dialogue.strip("\u201c\u201d\"'")
    prompt = PROMPT_TEMPLATE.format(dialogue=dialogue)

    img_bytes = context.user_data.get("image_bytes")
    if not img_bytes:
        await update.message.reply_text("Lost the image — let's restart. Tap “🎬 New video”.")
        return ConversationHandler.END

    status_msg = await update.message.reply_text("Uploading image…")
    timeout = aiohttp.ClientTimeout(total=POLL_TIMEOUT + 120)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            image_url = await upload_image(session, img_bytes)
            await status_msg.edit_text("Generating… 1–3 min.")
            task_id = await submit_job(session, prompt, image_url)
            await status_msg.edit_text(f"Working… (task {task_id[:18]})")
            video_url = await poll_job(session, task_id)
            data, size = await fetch_bytes(session, video_url)

            if data is None:
                await status_msg.edit_text(
                    f"Video is {size/1_048_576:.1f} MB — too big for Telegram upload.\n"
                    f"Direct link (expires in 24h, save now):\n{video_url}"
                )
            else:
                await update.message.reply_video(
                    video=data, caption=dialogue[:200], supports_streaming=True
                )
                await status_msg.delete()
    except Exception as e:  # noqa: BLE001
        log.exception("generation failed")
        await status_msg.edit_text(f"Failed: {e}")
    finally:
        context.user_data.pop("image_bytes", None)

    await update.message.reply_text("Done. Tap “🎬 New video” for another.",
                                    reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("image_bytes", None)
    await update.message.reply_text("Cancelled.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("go", go),
            MessageHandler(filters.Regex("^🎬 New video$"), go),
        ],
        states={
            ASK_IMAGE: [MessageHandler(
                (filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND, got_image
            )],
            ASK_DIALOGUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_dialogue)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    log.info("Bot starting (long-polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
