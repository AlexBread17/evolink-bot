"""
EvoLink Seedance 2.0 Fast (image-to-video) Telegram bot.

Three buttons from the main menu:

  ⚡ Quick Mode
      Fixed default image + fixed prompt template + 480p + 4s.
      You type ONLY the dialogue line, confirm, and it generates.

  🎛️ Flexible Mode
      You send an image, then choose how the prompt works:
        💬 Dialogue box  -> type only the dialogue (uses the template)
        📝 Full prompt   -> type the entire prompt yourself
      Quality is locked to 480p, duration to 4s.

  ⚙️ Settings
      Set / replace the default image used by Quick Mode (no commands needed).

Captions on finished videos:
  - Quick Mode and Flexible→Dialogue: caption = dialogue line + credits used.
  - Flexible→Full prompt: caption = credits used ONLY (prompt is never shown).

Clean interface: the bot deletes its own step messages as you progress. The
finished video and its caption are NEVER deleted. (Telegram only lets bots
delete messages younger than 48h, which always covers a live session.)

The default image is stored as an EvoLink file URL (survives restarts and skips
re-uploading on every Quick Mode run).
"""

import os
import json
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
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
EVOLINK_KEY = os.environ["EVOLINK_API_KEY"]
ALLOWED_USERS = {
    int(uid) for uid in os.environ.get("ALLOWED_USERS", "").split(",") if uid.strip()
}

# Fixed generation settings.
MODEL = "seedance-2.0-fast-image-to-video"
DURATION = 4              # seconds (locked)
ASPECT_RATIO = "9:16"     # vertical (locked)
QUALITY = "480p"          # locked to 480p in both modes
GENERATE_AUDIO = True
CONTENT_FILTER = False    # unrestricted in all modes (+10% billing)

# Fixed prompt template. {dialogue} is replaced by the line you type.
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
CREDITS_URL = "https://api.evolink.ai/v1/credits"

# Where the Quick Mode default image URL is persisted.
STATE_FILE = os.environ.get("STATE_FILE", "/tmp/evolink_bot_state.json")

# Polling
POLL_INTERVAL = 5
POLL_TIMEOUT = 600        # 10 min

# Telegram bot-upload ceiling
TG_UPLOAD_LIMIT = 50 * 1024 * 1024

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("evolink-bot")

# ---- Button labels (single source of truth) ----
BTN_QUICK = "⚡ Quick Mode"
BTN_FLEX = "🎛️ Flexible Mode"
BTN_SETTINGS = "⚙️ Settings"
BTN_BACK = "⬅️ Back"
BTN_CANCEL = "❌ Cancel"
BTN_GENERATE = "✅ Generate"
BTN_SET_IMAGE = "🖼️ Set default image"
BTN_DIALOGUE = "💬 Dialogue box"
BTN_FULLPROMPT = "📝 Full prompt"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_QUICK, BTN_FLEX], [BTN_SETTINGS]],
    resize_keyboard=True, one_time_keyboard=False,
)
CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_CANCEL]], resize_keyboard=True, one_time_keyboard=False
)
BACK_CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_BACK, BTN_CANCEL]], resize_keyboard=True, one_time_keyboard=False
)
PROMPTTYPE_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_DIALOGUE, BTN_FULLPROMPT], [BTN_BACK, BTN_CANCEL]],
    resize_keyboard=True, one_time_keyboard=False,
)
CONFIRM_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_GENERATE], [BTN_BACK, BTN_CANCEL]],
    resize_keyboard=True, one_time_keyboard=False,
)
SETTINGS_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_SET_IMAGE], [BTN_BACK]], resize_keyboard=True, one_time_keyboard=False
)

# Conversation states
(QUICK_DIALOGUE, QUICK_CONFIRM,
 FLEX_IMAGE, FLEX_PROMPTTYPE, FLEX_TEXT, FLEX_CONFIRM,
 SET_IMAGE_WAIT) = range(7)


def authorised(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and user.id in ALLOWED_USERS


# ---------------------------------------------------------------------------
# Persistent default-image storage (EvoLink URL only)
# ---------------------------------------------------------------------------
def load_default_image() -> str | None:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f).get("default_image_url")
    except Exception:  # noqa: BLE001
        return None


def save_default_image(url: str) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"default_image_url": url}, f)
    except Exception:  # noqa: BLE001
        log.warning("could not persist default image to %s", STATE_FILE)


# ---------------------------------------------------------------------------
# EvoLink calls  (unchanged, proven)
# ---------------------------------------------------------------------------
async def upload_image(session: aiohttp.ClientSession, img_bytes: bytes,
                       filename: str = "frame.jpg") -> str:
    form = aiohttp.FormData()
    form.add_field("file", img_bytes, filename=filename, content_type="image/jpeg")
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
                     image_url: str, quality: str = QUALITY) -> str:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "image_urls": [image_url],
        "duration": DURATION,
        "quality": quality,
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


async def get_credits(session: aiohttp.ClientSession):
    headers = {"Authorization": f"Bearer {EVOLINK_KEY}"}
    try:
        async with session.get(CREDITS_URL, headers=headers) as r:
            body = await r.json()
            user = (body.get("data") or {}).get("user") or {}
            val = user.get("remaining_credits")
            return float(val) if val is not None else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Message cleanup helpers
# ---------------------------------------------------------------------------
def track(context: ContextTypes.DEFAULT_TYPE, *message_ids: int) -> None:
    """Remember bot/user message ids that should be wiped at cleanup."""
    bucket = context.user_data.setdefault("trash", [])
    bucket.extend(m for m in message_ids if m)


async def cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete all tracked step messages. Videos/captions are never tracked."""
    chat_id = update.effective_chat.id
    for mid in context.user_data.get("trash", []):
        try:
            await context.bot.delete_message(chat_id, mid)
        except Exception:  # noqa: BLE001 - message too old / already gone
            pass
    context.user_data["trash"] = []


async def say(update, context, text, keyboard=None):
    """Send a tracked step message (so it can be cleaned up later)."""
    msg = await update.message.reply_text(text, reply_markup=keyboard)
    track(context, msg.message_id)
    return msg


# ---------------------------------------------------------------------------
# Generation core (shared by all modes)
# ---------------------------------------------------------------------------
async def run_generation(update, context, *, prompt, caption, image_url=None,
                         image_bytes=None):
    """Upload (if needed), generate, send the video with the given caption.

    Exactly one of image_url / image_bytes must be provided. The status message
    is tracked + deleted; the final video is NOT tracked (stays in chat).
    """
    status = await update.message.reply_text("Uploading…")
    timeout = aiohttp.ClientTimeout(total=POLL_TIMEOUT + 120)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            credits_before = await get_credits(session)
            if image_url is None:
                image_url = await upload_image(session, image_bytes)
            await status.edit_text("Generating… 1–3 min.")
            task_id = await submit_job(session, prompt, image_url, QUALITY)
            await status.edit_text(f"Working… (task {task_id[:18]})")
            video_url = await poll_job(session, task_id)
            data, size = await fetch_bytes(session, video_url)

            credits_after = await get_credits(session)
            if credits_after is not None and credits_before is not None:
                used = max(credits_before - credits_after, 0)
                credit_line = f"💳 Used {used:.2f} credits · {credits_after:.2f} left"
            elif credits_after is not None:
                credit_line = f"💳 {credits_after:.2f} credits left"
            else:
                credit_line = ""

            full_caption = (f"{caption}\n{credit_line}".strip()
                            if caption else credit_line)

            if data is None:
                # Too big to upload: send the link as a NORMAL (kept) message.
                await update.message.reply_text(
                    f"Video is {size/1_048_576:.1f} MB — too big for Telegram.\n"
                    f"Link (expires 24h, save now):\n{video_url}\n{credit_line}"
                )
            else:
                # The video is sent UNtracked => never deleted.
                await update.message.reply_video(
                    video=data, caption=full_caption or None,
                    supports_streaming=True,
                )
            return True
    except Exception as e:  # noqa: BLE001
        log.exception("generation failed")
        # keep the error visible (untracked) so the user sees what happened
        await update.message.reply_text(f"Failed: {e}")
        return False
    finally:
        # Remove the "Uploading/Generating" status line specifically.
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Entry points / menu
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    has_img = "yes" if load_default_image() else "no"
    await update.message.reply_text(
        "Pick a mode:\n"
        f"⚡ Quick — type only the dialogue (uses your default image; set: {has_img}).\n"
        "🎛️ Flexible — choose an image, then dialogue box or full prompt.\n"
        "⚙️ Settings — set the Quick Mode default image.\n\n"
        "All clips are 480p · 4s · 9:16. Step messages auto-clear; videos stay.",
        reply_markup=MAIN_KEYBOARD,
    )


# ---------- QUICK MODE ----------
async def quick_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    context.user_data.clear()
    if not load_default_image():
        await update.message.reply_text(
            "No default image yet. Set one in ⚙️ Settings first.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END
    track(context, update.message.message_id)
    await say(update, context, "Quick Mode. Type the dialogue line.", CANCEL_KEYBOARD)
    return QUICK_DIALOGUE


async def quick_dialogue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    dialogue = (update.message.text or "").strip().strip("\u201c\u201d\"'")
    if not dialogue:
        await say(update, context, "Send the dialogue line as text.", CANCEL_KEYBOARD)
        return QUICK_DIALOGUE
    context.user_data["dialogue"] = dialogue
    await say(update, context,
              f"Generate this line?\n“{dialogue}”", CONFIRM_KEYBOARD)
    return QUICK_CONFIRM


async def quick_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    if update.message.text == BTN_BACK:
        await say(update, context, "Type the dialogue line.", CANCEL_KEYBOARD)
        return QUICK_DIALOGUE
    if update.message.text != BTN_GENERATE:
        await say(update, context, "Tap ✅ Generate, ⬅️ Back, or ❌ Cancel.",
                  CONFIRM_KEYBOARD)
        return QUICK_CONFIRM

    dialogue = context.user_data.get("dialogue")
    image_url = load_default_image()
    if not dialogue or not image_url:
        await cleanup(update, context)
        await update.message.reply_text("Lost the details — start again.",
                                        reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    await cleanup(update, context)  # wipe step chatter before sending the video
    prompt = PROMPT_TEMPLATE.format(dialogue=dialogue)
    await run_generation(update, context, prompt=prompt, caption=dialogue,
                         image_url=image_url)
    await update.message.reply_text("Done. Pick a mode for another.",
                                    reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ---------- FLEXIBLE MODE ----------
async def flex_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    context.user_data.clear()
    track(context, update.message.message_id)
    await say(update, context, "Flexible Mode. Send the image (as a photo).",
              CANCEL_KEYBOARD)
    return FLEX_IMAGE


async def flex_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    file_id = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        file_id = update.message.document.file_id
    if not file_id:
        await say(update, context, "That wasn't an image. Send a photo.", CANCEL_KEYBOARD)
        return FLEX_IMAGE
    tg_file = await context.bot.get_file(file_id)
    context.user_data["image_bytes"] = bytes(await tg_file.download_as_bytearray())
    await say(update, context, "Prompt type?", PROMPTTYPE_KEYBOARD)
    return FLEX_PROMPTTYPE


async def flex_prompttype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    choice = update.message.text
    if choice == BTN_BACK:
        context.user_data.pop("image_bytes", None)
        await say(update, context, "Send the image (as a photo).", CANCEL_KEYBOARD)
        return FLEX_IMAGE
    if choice == BTN_DIALOGUE:
        context.user_data["mode"] = "dialogue"
        await say(update, context, "Type the dialogue line.", BACK_CANCEL_KEYBOARD)
        return FLEX_TEXT
    if choice == BTN_FULLPROMPT:
        context.user_data["mode"] = "full"
        await say(update, context, "Type the full prompt.", BACK_CANCEL_KEYBOARD)
        return FLEX_TEXT
    await say(update, context, "Tap 💬 Dialogue box or 📝 Full prompt.",
              PROMPTTYPE_KEYBOARD)
    return FLEX_PROMPTTYPE


async def flex_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    if update.message.text == BTN_BACK:
        context.user_data.pop("mode", None)
        await say(update, context, "Prompt type?", PROMPTTYPE_KEYBOARD)
        return FLEX_PROMPTTYPE
    text = (update.message.text or "").strip()
    if not text:
        await say(update, context, "Send it as text.", BACK_CANCEL_KEYBOARD)
        return FLEX_TEXT

    if context.user_data.get("mode") == "dialogue":
        text = text.strip("\u201c\u201d\"'")
    context.user_data["text"] = text

    preview = (f"Generate this line?\n“{text}”"
               if context.user_data.get("mode") == "dialogue"
               else "Generate with your full prompt?")
    await say(update, context, preview, CONFIRM_KEYBOARD)
    return FLEX_CONFIRM


async def flex_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    if update.message.text == BTN_BACK:
        mode = context.user_data.get("mode")
        label = "dialogue line" if mode == "dialogue" else "full prompt"
        await say(update, context, f"Type the {label}.", BACK_CANCEL_KEYBOARD)
        return FLEX_TEXT
    if update.message.text != BTN_GENERATE:
        await say(update, context, "Tap ✅ Generate, ⬅️ Back, or ❌ Cancel.",
                  CONFIRM_KEYBOARD)
        return FLEX_CONFIRM

    text = context.user_data.get("text")
    mode = context.user_data.get("mode")
    img_bytes = context.user_data.get("image_bytes")
    if not text or not mode or not img_bytes:
        await cleanup(update, context)
        await update.message.reply_text("Lost the details — start again.",
                                        reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    if mode == "dialogue":
        prompt = PROMPT_TEMPLATE.format(dialogue=text)
        caption = text                      # dialogue shown
    else:
        prompt = text
        caption = ""                        # full prompt: credits only, prompt hidden

    await cleanup(update, context)
    await run_generation(update, context, prompt=prompt, caption=caption,
                         image_bytes=img_bytes)
    await update.message.reply_text("Done. Pick a mode for another.",
                                    reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ---------- SETTINGS ----------
async def settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    context.user_data.clear()
    track(context, update.message.message_id)
    state = "set ✅" if load_default_image() else "not set ❌"
    await say(update, context,
              f"Settings. Quick Mode default image: {state}.",
              SETTINGS_KEYBOARD)
    return SET_IMAGE_WAIT


async def settings_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the Settings menu: either a button tap or an incoming photo."""
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)

    if update.message.text == BTN_BACK:
        await cleanup(update, context)
        await update.message.reply_text("Back to menu.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    if update.message.text == BTN_SET_IMAGE:
        await say(update, context, "Send the photo to use as the default.",
                  BACK_CANCEL_KEYBOARD)
        return SET_IMAGE_WAIT

    # A photo: upload to EvoLink once, store the URL.
    file_id = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        file_id = update.message.document.file_id

    if not file_id:
        await say(update, context, "Send a photo, or tap 🖼️ / ⬅️ Back.",
                  SETTINGS_KEYBOARD)
        return SET_IMAGE_WAIT

    tg_file = await context.bot.get_file(file_id)
    img_bytes = bytes(await tg_file.download_as_bytearray())
    status = await update.message.reply_text("Saving default image…")
    track(context, status.message_id)
    try:
        async with aiohttp.ClientSession() as session:
            url = await upload_image(session, img_bytes)
        save_default_image(url)
        await cleanup(update, context)
        await update.message.reply_text("✅ Default image saved.",
                                        reply_markup=MAIN_KEYBOARD)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Couldn't save image: {e}",
                                        reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ---------- shared cancel ----------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track(context, update.message.message_id)
    await cleanup(update, context)
    await update.message.reply_text("Cancelled.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    async with aiohttp.ClientSession() as session:
        bal = await get_credits(session)
    if bal is None:
        await update.message.reply_text("Couldn't fetch the balance right now.")
    else:
        await update.message.reply_text(f"💳 {bal:.2f} credits remaining.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    cancel_filter = filters.Regex(f"^{BTN_CANCEL}$")
    txt = filters.TEXT & ~filters.COMMAND & ~cancel_filter
    img = (filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_QUICK}$"), quick_start),
            MessageHandler(filters.Regex(f"^{BTN_FLEX}$"), flex_start),
            MessageHandler(filters.Regex(f"^{BTN_SETTINGS}$"), settings_start),
        ],
        states={
            QUICK_DIALOGUE: [MessageHandler(txt, quick_dialogue)],
            QUICK_CONFIRM:  [MessageHandler(txt, quick_confirm)],
            FLEX_IMAGE:     [MessageHandler(img | txt, flex_image)],
            FLEX_PROMPTTYPE:[MessageHandler(txt, flex_prompttype)],
            FLEX_TEXT:      [MessageHandler(txt, flex_text)],
            FLEX_CONFIRM:   [MessageHandler(txt, flex_confirm)],
            SET_IMAGE_WAIT: [MessageHandler(img | txt, settings_router)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(cancel_filter, cancel),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(conv)
    log.info("Bot starting (long-polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
