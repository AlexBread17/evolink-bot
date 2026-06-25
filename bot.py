"""
EvoLink Seedance 2.0 Fast (image-to-video) Telegram bot.

Three buttons from the main menu:

  ⚡ Quick Mode
      Fixed default image + fixed prompt template. You type ONLY the dialogue
      line, confirm, and it generates. Duration/quality/aspect come from Settings.

  🎛️ Flexible Mode
      You send an image, then choose how the prompt works:
        💬 Dialogue box  -> type only the dialogue (uses the template)
        📝 Full prompt   -> type the entire prompt yourself
      Duration, quality and aspect ratio all come from Settings.

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

# Generation settings. These are DEFAULTS; the live values are saved in state
# and changed only via the Settings menu.
MODEL = "seedance-2.0-fast-image-to-video"
DEFAULT_DURATION = 4          # seconds
DEFAULT_ASPECT_RATIO = "9:16" # vertical
DEFAULT_QUALITY = "480p"
GENERATE_AUDIO = True
CONTENT_FILTER = False        # unrestricted in all modes (+10% billing)

# Allowed options offered in Settings.
DURATION_OPTIONS = [4, 6, 8, 10, 15]          # seconds
QUALITY_OPTIONS = ["480p", "720p"]
ASPECT_OPTIONS = ["9:16", "16:9", "3:4", "4:3", "1:1", "21:9"]
# Friendly labels for aspect ratios.
ASPECT_LABELS = {
    "9:16": "9:16 vertical",
    "16:9": "16:9 horizontal",
    "3:4": "3:4 portrait",
    "4:3": "4:3 landscape",
    "1:1": "1:1 square",
    "21:9": "21:9 cinematic",
}
# Rough credits-per-second by quality (for cost hints; actuals vary slightly).
QUALITY_CREDITS_PER_SEC = {"480p": 5.6, "720p": 13.5}

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
BTN_QUICK = "⚡ Quick"
BTN_FLEX = "🎛️ Flex"
BTN_SETTINGS = "⚙️ Settings"
BTN_BACK = "⬅️ Back"
BTN_CANCEL = "❌ Cancel"
BTN_GENERATE = "✅ Go"
BTN_SET_IMAGE = "🖼️ Image"
BTN_EDIT_PROMPT = "📝 Edit"
BTN_VIEW_PROMPT = "👁️ View"
BTN_RESET_PROMPT = "♻️ Reset"
BTN_SET_DURATION = "⏱️ Length"
BTN_SET_QUALITY = "🎚️ Quality"
BTN_SET_ASPECT = "📐 Aspect"
BTN_DIALOGUE = "💬 Dialogue"
BTN_FULLPROMPT = "📝 Prompt"

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
    [[BTN_SET_DURATION, BTN_SET_QUALITY, BTN_SET_ASPECT],
     [BTN_SET_IMAGE, BTN_EDIT_PROMPT],
     [BTN_VIEW_PROMPT, BTN_RESET_PROMPT],
     [BTN_BACK]],
    resize_keyboard=True, one_time_keyboard=False,
)
# Pickers (built dynamically from the option lists).
DURATION_KEYBOARD = ReplyKeyboardMarkup(
    [[f"{d}s" for d in DURATION_OPTIONS], [BTN_BACK]],
    resize_keyboard=True, one_time_keyboard=False,
)
QUALITY_KEYBOARD = ReplyKeyboardMarkup(
    [QUALITY_OPTIONS, [BTN_BACK]],
    resize_keyboard=True, one_time_keyboard=False,
)
ASPECT_KEYBOARD = ReplyKeyboardMarkup(
    [ASPECT_OPTIONS[:3], ASPECT_OPTIONS[3:], [BTN_BACK]],
    resize_keyboard=True, one_time_keyboard=False,
)

# Conversation states
(QUICK_DIALOGUE, QUICK_CONFIRM,
 FLEX_IMAGE, FLEX_PROMPTTYPE, FLEX_TEXT, FLEX_CONFIRM,
 SET_IMAGE_WAIT, EDIT_PROMPT_WAIT,
 PICK_DURATION, PICK_QUALITY, PICK_ASPECT) = range(11)


def authorised(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and user.id in ALLOWED_USERS


# ---------------------------------------------------------------------------
# Persistent state (default image URL + custom prompt template)
# ---------------------------------------------------------------------------
def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:  # noqa: BLE001
        log.warning("could not persist state to %s", STATE_FILE)


def load_default_image() -> str | None:
    return _load_state().get("default_image_url")


def save_default_image(url: str) -> None:
    state = _load_state()
    state["default_image_url"] = url
    _save_state(state)


def load_template() -> str:
    """Saved custom template if set, else the built-in PROMPT_TEMPLATE."""
    return _load_state().get("prompt_template") or PROMPT_TEMPLATE


def save_template(text: str) -> None:
    state = _load_state()
    state["prompt_template"] = text
    _save_state(state)


def reset_template() -> None:
    state = _load_state()
    state.pop("prompt_template", None)
    _save_state(state)


def get_duration() -> int:
    val = _load_state().get("duration")
    return val if val in DURATION_OPTIONS else DEFAULT_DURATION


def set_duration(seconds: int) -> None:
    state = _load_state()
    state["duration"] = seconds
    _save_state(state)


def get_quality() -> str:
    val = _load_state().get("quality")
    return val if val in QUALITY_OPTIONS else DEFAULT_QUALITY


def set_quality(q: str) -> None:
    state = _load_state()
    state["quality"] = q
    _save_state(state)


def get_aspect() -> str:
    val = _load_state().get("aspect_ratio")
    return val if val in ASPECT_OPTIONS else DEFAULT_ASPECT_RATIO


def set_aspect(a: str) -> None:
    state = _load_state()
    state["aspect_ratio"] = a
    _save_state(state)


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
                     image_url: str) -> str:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "image_urls": [image_url],
        "duration": get_duration(),
        "quality": get_quality(),
        "aspect_ratio": get_aspect(),
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


async def clear_close(update, context):
    """Delete the lingering closing message (if any) as a new flow starts."""
    prev = context.chat_data.get("last_close_id")
    if prev:
        try:
            await context.bot.delete_message(update.effective_chat.id, prev)
        except Exception:  # noqa: BLE001
            pass
        context.chat_data["last_close_id"] = None


async def close_msg(update, context, text):
    """Send a 'closing' message (Done / Cancelled / Saved / Back to menu) and
    delete the PREVIOUS closing message, so at most one ever lingers. Stored in
    chat_data because it must survive user_data.clear() between conversations."""
    prev = context.chat_data.get("last_close_id")
    if prev:
        try:
            await context.bot.delete_message(update.effective_chat.id, prev)
        except Exception:  # noqa: BLE001
            pass
    msg = await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
    context.chat_data["last_close_id"] = msg.message_id
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
            task_id = await submit_job(session, prompt, image_url)
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
        "⚙️ Settings — duration, quality, aspect, image, template.\n\n"
        f"Current: {get_quality()} · {get_duration()}s · {get_aspect()}. "
        "Step messages auto-clear; videos stay.",
        reply_markup=MAIN_KEYBOARD,
    )


# ---------- QUICK MODE ----------
async def quick_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    context.user_data.clear()
    await clear_close(update, context)
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
        await say(update, context,
                  f"Tap {BTN_GENERATE}, {BTN_BACK}, or {BTN_CANCEL}.",
                  CONFIRM_KEYBOARD)
        return QUICK_CONFIRM

    dialogue = context.user_data.get("dialogue")
    image_url = load_default_image()
    if not dialogue or not image_url:
        await cleanup(update, context)
        await close_msg(update, context, "Lost the details — start again.")
        return ConversationHandler.END

    await cleanup(update, context)  # wipe step chatter before sending the video
    prompt = load_template().format(dialogue=dialogue)
    await run_generation(update, context, prompt=prompt, caption=dialogue,
                         image_url=image_url)
    await close_msg(update, context, "Done. Pick a mode for another.")
    return ConversationHandler.END


# ---------- FLEXIBLE MODE ----------
async def flex_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    context.user_data.clear()
    await clear_close(update, context)
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
    await say(update, context, f"Tap {BTN_DIALOGUE} or {BTN_FULLPROMPT}.",
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
        await say(update, context,
                  f"Tap {BTN_GENERATE}, {BTN_BACK}, or {BTN_CANCEL}.",
                  CONFIRM_KEYBOARD)
        return FLEX_CONFIRM

    text = context.user_data.get("text")
    mode = context.user_data.get("mode")
    img_bytes = context.user_data.get("image_bytes")
    if not text or not mode or not img_bytes:
        await cleanup(update, context)
        await close_msg(update, context, "Lost the details — start again.")
        return ConversationHandler.END

    if mode == "dialogue":
        prompt = load_template().format(dialogue=text)
        caption = text                      # dialogue shown
    else:
        prompt = text
        caption = ""                        # full prompt: credits only, prompt hidden

    await cleanup(update, context)
    await run_generation(update, context, prompt=prompt, caption=caption,
                         image_bytes=img_bytes)
    await close_msg(update, context, "Done. Pick a mode for another.")
    return ConversationHandler.END


# ---------- SETTINGS ----------
def settings_overview_text() -> str:
    img = "set ✅" if load_default_image() else "not set ❌"
    tpl = "custom" if _load_state().get("prompt_template") else "default"
    dur = get_duration()
    qual = get_quality()
    asp = ASPECT_LABELS.get(get_aspect(), get_aspect())
    cps = QUALITY_CREDITS_PER_SEC.get(qual, 0)
    est = dur * cps
    return (
        "⚙️ Settings — current setup\n"
        "──────────────\n"
        f"⏱️ Duration  :  {dur}s\n"
        f"🎚️ Quality   :  {qual}\n"
        f"📐 Aspect    :  {ASPECT_LABELS.get(get_aspect(), get_aspect())}\n"
        f"🖼️ Image     :  {img}\n"
        f"📝 Template  :  {tpl}\n"
        "──────────────\n"
        f"≈ {est:.0f} credits per clip at these settings.\n\n"
        "Tap a button to change it."
    )


async def show_settings(update, context):
    await say(update, context, settings_overview_text(), SETTINGS_KEYBOARD)
    return SET_IMAGE_WAIT


async def settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    context.user_data.clear()
    await clear_close(update, context)
    track(context, update.message.message_id)
    return await show_settings(update, context)


async def settings_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the Settings menu: either a button tap or an incoming photo."""
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)

    if update.message.text == BTN_BACK:
        await cleanup(update, context)
        await close_msg(update, context, "Back to menu.")
        return ConversationHandler.END

    if update.message.text == BTN_SET_DURATION:
        await say(update, context,
                  f"Pick clip length (current: {get_duration()}s).",
                  DURATION_KEYBOARD)
        return PICK_DURATION

    if update.message.text == BTN_SET_QUALITY:
        hints = " · ".join(
            f"{q} ≈{QUALITY_CREDITS_PER_SEC.get(q,0):.1f} cr/s" for q in QUALITY_OPTIONS
        )
        await say(update, context,
                  f"Pick quality (current: {get_quality()}).\n{hints}",
                  QUALITY_KEYBOARD)
        return PICK_QUALITY

    if update.message.text == BTN_SET_ASPECT:
        legend = "\n".join(f"• {a} — {ASPECT_LABELS[a].split(' ',1)[1]}"
                           for a in ASPECT_OPTIONS)
        await say(update, context,
                  f"Pick aspect ratio (current: {get_aspect()}).\n{legend}",
                  ASPECT_KEYBOARD)
        return PICK_ASPECT

    if update.message.text == BTN_SET_IMAGE:
        await say(update, context, "Send the photo to use as the default.",
                  BACK_CANCEL_KEYBOARD)
        return SET_IMAGE_WAIT

    if update.message.text == BTN_VIEW_PROMPT:
        # Show the active template, tracked so it clears on Back/Cancel/next action.
        m = await update.message.reply_text(
            "Current template (use {dialogue} where the line goes):\n\n"
            + load_template()
        )
        track(context, m.message_id)
        await say(update, context, "Anything else?", SETTINGS_KEYBOARD)
        return SET_IMAGE_WAIT

    if update.message.text == BTN_RESET_PROMPT:
        reset_template()
        await say(update, context, "♻️ Template reset to the built-in default.",
                  SETTINGS_KEYBOARD)
        return await show_settings(update, context)

    if update.message.text == BTN_EDIT_PROMPT:
        await say(update, context,
                  "Send the new prompt template as one message.\n"
                  "It MUST contain {dialogue} where the spoken line should go "
                  "(curly braces, lowercase). Example ending:\n"
                  'Dialogue: \u201c{dialogue}\u201d',
                  BACK_CANCEL_KEYBOARD)
        return EDIT_PROMPT_WAIT

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
        await close_msg(update, context, "✅ Default image saved.")
    except Exception as e:  # noqa: BLE001
        await close_msg(update, context, f"Couldn't save image: {e}")
    return ConversationHandler.END


async def edit_prompt_wait(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive a new prompt template; require a {dialogue} placeholder."""
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)

    if update.message.text == BTN_BACK:
        return await settings_start(update, context)

    text = (update.message.text or "").strip()
    if "{dialogue}" not in text:
        await say(update, context,
                  "That template has no {dialogue} placeholder, so the dialogue "
                  "line would have nowhere to go. Add {dialogue} and resend, or "
                  "tap ⬅️ Back.",
                  BACK_CANCEL_KEYBOARD)
        return EDIT_PROMPT_WAIT

    # Guard against a malformed template that would crash .format()
    try:
        test = text.format(dialogue="TEST")
    except Exception:  # noqa: BLE001
        await say(update, context,
                  "That template has a formatting problem (stray { or } besides "
                  "{dialogue}). Fix it and resend, or tap ⬅️ Back.",
                  BACK_CANCEL_KEYBOARD)
        return EDIT_PROMPT_WAIT

    save_template(text)
    await cleanup(update, context)
    await close_msg(update, context, "✅ Prompt template saved.")
    return ConversationHandler.END


async def pick_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    if update.message.text == BTN_BACK:
        return await show_settings(update, context)
    raw = (update.message.text or "").strip().rstrip("s")
    if not raw.isdigit() or int(raw) not in DURATION_OPTIONS:
        await say(update, context, "Tap one of the durations, or ⬅️ Back.",
                  DURATION_KEYBOARD)
        return PICK_DURATION
    set_duration(int(raw))
    return await show_settings(update, context)


async def pick_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    if update.message.text == BTN_BACK:
        return await show_settings(update, context)
    q = (update.message.text or "").strip()
    if q not in QUALITY_OPTIONS:
        await say(update, context, "Tap 480p or 720p, or ⬅️ Back.", QUALITY_KEYBOARD)
        return PICK_QUALITY
    set_quality(q)
    return await show_settings(update, context)


async def pick_aspect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    if update.message.text == BTN_BACK:
        return await show_settings(update, context)
    a = (update.message.text or "").strip()
    if a not in ASPECT_OPTIONS:
        await say(update, context, "Tap one of the ratios, or ⬅️ Back.", ASPECT_KEYBOARD)
        return PICK_ASPECT
    set_aspect(a)
    return await show_settings(update, context)


# ---------- shared cancel ----------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track(context, update.message.message_id)
    await cleanup(update, context)
    await close_msg(update, context, "Cancelled.")
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
            EDIT_PROMPT_WAIT: [MessageHandler(txt, edit_prompt_wait)],
            PICK_DURATION: [MessageHandler(txt, pick_duration)],
            PICK_QUALITY: [MessageHandler(txt, pick_quality)],
            PICK_ASPECT: [MessageHandler(txt, pick_aspect)],
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
