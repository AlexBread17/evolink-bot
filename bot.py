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
import uuid
import aiohttp

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
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
IMAGE_USERS = {
    int(uid) for uid in os.environ.get("IMAGE_USERS", "").split(",") if uid.strip()
}

# Generation settings. These are DEFAULTS; the live values are saved in state
# and changed only via the Settings menu.
MODEL = "seedance-2.0-fast-image-to-video"
IMAGE_MODEL = "doubao-seedream-5.0-pro"
IMAGE_GEN_URL = "https://api.evolink.ai/v1/images/generations"
DEFAULT_IMAGE_QUALITY = "2K"
DEFAULT_IMAGE_SIZE = "auto"
IMAGE_QUALITY_OPTIONS = ["1K", "2K"]
IMAGE_SIZE_OPTIONS = ["auto", "1:1", "9:16", "16:9", "3:4", "4:3", "4:5", "5:4", "21:9"]
DEFAULT_DURATION = 4          # seconds
DEFAULT_ASPECT_RATIO = "9:16" # vertical
DEFAULT_QUALITY = "480p"
DEFAULT_COUNT = 1
GENERATE_AUDIO = True
CONTENT_FILTER = False        # unrestricted in all modes (+10% billing)

# Allowed options offered in Settings.
DURATION_OPTIONS = [4, 6, 8, 10, 15]          # seconds
QUALITY_OPTIONS = ["480p", "720p"]
COUNT_OPTIONS = [1, 2]                          # videos per submit
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

# In-memory cache for image repeat jobs (key -> params dict).
_repeat_cache: dict[str, dict] = {}

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
BTN_IMG_EDIT = "🖌️ Edit"
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
BTN_SET_COUNT = "🔢 Count"
BTN_SET_IMG_QUALITY = "🖼️ Img Res"
BTN_SET_IMG_SIZE = "📐 Img Size"
BTN_DIALOGUE = "💬 Dialogue"
BTN_FULLPROMPT = "📝 Prompt"
BTN_NEXT = "▶️"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_QUICK, BTN_FLEX], [BTN_IMG_EDIT, BTN_SETTINGS]],
    resize_keyboard=True, one_time_keyboard=False,
)
IMAGE_ONLY_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_IMG_EDIT]],
    resize_keyboard=True, one_time_keyboard=False,
)
CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_CANCEL]], resize_keyboard=True, one_time_keyboard=False
)
BACK_CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_BACK, BTN_CANCEL]], resize_keyboard=True, one_time_keyboard=False
)
REFS_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_NEXT], [BTN_BACK, BTN_CANCEL]],
    resize_keyboard=True, one_time_keyboard=False,
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
     [BTN_SET_COUNT, BTN_SET_IMAGE],
     [BTN_SET_IMG_QUALITY, BTN_SET_IMG_SIZE],
     [BTN_EDIT_PROMPT, BTN_VIEW_PROMPT, BTN_RESET_PROMPT],
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
COUNT_KEYBOARD = ReplyKeyboardMarkup(
    [[str(c) for c in COUNT_OPTIONS], [BTN_BACK]],
    resize_keyboard=True, one_time_keyboard=False,
)
IMG_QUALITY_KEYBOARD = ReplyKeyboardMarkup(
    [IMAGE_QUALITY_OPTIONS, [BTN_BACK]],
    resize_keyboard=True, one_time_keyboard=False,
)
IMG_SIZE_KEYBOARD = ReplyKeyboardMarkup(
    [IMAGE_SIZE_OPTIONS[:3], IMAGE_SIZE_OPTIONS[3:6], IMAGE_SIZE_OPTIONS[6:], [BTN_BACK]],
    resize_keyboard=True, one_time_keyboard=False,
)

# Conversation states
(QUICK_DIALOGUE, QUICK_CONFIRM,
 FLEX_IMAGE, FLEX_PROMPTTYPE, FLEX_TEXT, FLEX_CONFIRM,
 SET_IMAGE_WAIT, EDIT_PROMPT_WAIT,
 PICK_DURATION, PICK_QUALITY, PICK_ASPECT, PICK_COUNT,
 PICK_IMG_QUALITY, PICK_IMG_SIZE,
 IMG_EDIT_PHOTO, IMG_EDIT_REFS, IMG_EDIT_TEXT, IMG_EDIT_CONFIRM) = range(18)


def authorised(update: Update) -> bool:
    """Full access: video + image modes."""
    user = update.effective_user
    return bool(user) and user.id in ALLOWED_USERS


def image_authorised(update: Update) -> bool:
    """Image edit access: ALLOWED_USERS (full) + IMAGE_USERS (image only)."""
    user = update.effective_user
    return bool(user) and user.id in (ALLOWED_USERS | IMAGE_USERS)


def get_user_keyboard(update: Update):
    """Return the right main keyboard for this user's access level."""
    user = update.effective_user
    if user and user.id in ALLOWED_USERS:
        return MAIN_KEYBOARD
    return IMAGE_ONLY_KEYBOARD


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


def get_count() -> int:
    val = _load_state().get("count")
    return val if val in COUNT_OPTIONS else DEFAULT_COUNT


def set_count(n: int) -> None:
    state = _load_state()
    state["count"] = n
    _save_state(state)


def get_image_quality() -> str:
    val = _load_state().get("image_quality")
    return val if val in IMAGE_QUALITY_OPTIONS else DEFAULT_IMAGE_QUALITY


def set_image_quality(q: str) -> None:
    state = _load_state()
    state["image_quality"] = q
    _save_state(state)


def get_image_size() -> str:
    val = _load_state().get("image_size")
    return val if val in IMAGE_SIZE_OPTIONS else DEFAULT_IMAGE_SIZE


def set_image_size(s: str) -> None:
    state = _load_state()
    state["image_size"] = s
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
                     image_url: str, *, duration: int, quality: str,
                     aspect_ratio: str) -> str:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "image_urls": [image_url],
        "duration": duration,
        "quality": quality,
        "aspect_ratio": aspect_ratio,
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


async def close_msg(update, context, text, keyboard=None):
    """Send a 'closing' message (Done / Cancelled / Saved / Back to menu) and
    delete the PREVIOUS closing message, so at most one ever lingers. Stored in
    chat_data because it must survive user_data.clear() between conversations."""
    prev = context.chat_data.get("last_close_id")
    if prev:
        try:
            await context.bot.delete_message(update.effective_chat.id, prev)
        except Exception:  # noqa: BLE001
            pass
    kb = keyboard or get_user_keyboard(update)
    msg = await update.message.reply_text(text, reply_markup=kb)
    context.chat_data["last_close_id"] = msg.message_id
    return msg


# ---------------------------------------------------------------------------
# Generation core (shared by all modes) — runs as background task
# ---------------------------------------------------------------------------
async def run_generation(bot, chat_id, *, prompt, caption, image_url=None,
                         image_bytes=None, duration, quality, aspect_ratio, count):
    """Upload (if needed), generate COUNT clip(s), send each with the caption.

    Runs as an asyncio background task — does not block the conversation.
    Settings are snapshotted at confirm time so mid-generation changes don't leak.
    """
    status = await bot.send_message(chat_id,
        "Uploading…" if count == 1 else f"Uploading… (making {count} clips)"
    )
    timeout = aiohttp.ClientTimeout(total=POLL_TIMEOUT + 120)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            credits_before = await get_credits(session)
            if image_url is None:
                image_url = await upload_image(session, image_bytes)

            any_sent = False
            for i in range(count):
                tag = "" if count == 1 else f" {i+1}/{count}"
                await status.edit_text(f"Generating{tag}… 1–3 min.")
                try:
                    task_id = await submit_job(session, prompt, image_url,
                                              duration=duration, quality=quality,
                                              aspect_ratio=aspect_ratio)
                    await status.edit_text(f"Working{tag}… (task {task_id[:18]})")
                    video_url = await poll_job(session, task_id)
                    data, size = await fetch_bytes(session, video_url)
                except Exception as e:  # noqa: BLE001
                    log.exception("clip %d failed", i + 1)
                    await bot.send_message(chat_id, f"Clip{tag} failed: {e}")
                    continue

                base = f"{caption}{tag}" if caption else tag.strip()
                if data is None:
                    await bot.send_message(chat_id,
                        f"Video{tag} is {size/1_048_576:.1f} MB — too big for Telegram.\n"
                        f"Link (expires 24h, save now):\n{video_url}"
                    )
                else:
                    await bot.send_video(chat_id, video=data,
                                         caption=(base or None),
                                         supports_streaming=True)
                any_sent = True

            credits_after = await get_credits(session)
            if credits_after is not None and credits_before is not None:
                used = max(credits_before - credits_after, 0)
                summary = f"💳 Used {used:.2f} credits · {credits_after:.2f} left"
            elif credits_after is not None:
                summary = f"💳 {credits_after:.2f} credits left"
            else:
                summary = ""
            if summary:
                await bot.send_message(chat_id, summary)
            return any_sent
    except Exception as e:  # noqa: BLE001
        log.exception("generation failed")
        await bot.send_message(chat_id, f"Failed: {e}")
        return False
    finally:
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Image edit generation core
# ---------------------------------------------------------------------------
async def submit_image_job(session: aiohttp.ClientSession, prompt: str,
                           image_urls: list[str], *, img_size: str,
                           img_quality: str) -> str:
    payload = {
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "image_urls": image_urls,
        "size": img_size,
        "quality": img_quality,
        "n": 1,
        "output_format": "png",
        "watermark": False,
    }
    headers = {"Authorization": f"Bearer {EVOLINK_KEY}"}
    async with session.post(IMAGE_GEN_URL, json=payload, headers=headers) as r:
        body = await r.json()
        if r.status == 401:
            raise RuntimeError("EvoLink rejected the key (401).")
        if r.status == 402:
            raise RuntimeError("EvoLink balance too low (402). Top up credits.")
        if r.status >= 400:
            raise RuntimeError(f"Image submit error {r.status}: {body}")
        task_id = body.get("id")
        if not task_id:
            raise RuntimeError(f"No task id returned: {body}")
        return task_id


async def run_image_edit(bot, chat_id, *, prompt, image_bytes_list,
                         img_size, img_quality):
    """Upload image(s), submit Seedream edit job, poll, send result photo.
    image_bytes_list[0] = edit target, rest = references.
    Runs as an asyncio background task."""
    count = len(image_bytes_list)
    status = await bot.send_message(chat_id,
        f"Uploading {count} image{'s' if count > 1 else ''}…")
    timeout = aiohttp.ClientTimeout(total=POLL_TIMEOUT + 120)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            credits_before = await get_credits(session)
            image_urls = []
            for i, img_bytes in enumerate(image_bytes_list):
                url = await upload_image(session, img_bytes,
                                         filename=f"img_{i}.jpg")
                image_urls.append(url)

            await status.edit_text("Editing image… 10–45s.")
            task_id = await submit_image_job(session, prompt, image_urls,
                                              img_size=img_size,
                                              img_quality=img_quality)
            await status.edit_text(f"Working… (task {task_id[:18]})")
            result_url = await poll_job(session, task_id)

            # Cache params for repeat
            rid = uuid.uuid4().hex[:12]
            _repeat_cache[rid] = {
                "prompt": prompt,
                "image_bytes_list": image_bytes_list,
                "img_size": img_size,
                "img_quality": img_quality,
                "chat_id": chat_id,
            }
            repeat_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔁", callback_data=f"rep:{rid}")]]
            )

            data, size = await fetch_bytes(session, result_url)
            if data is None:
                await bot.send_message(chat_id,
                    f"Image is {size/1_048_576:.1f} MB — too big for Telegram.\n"
                    f"Link (expires 24h):\n{result_url}",
                    reply_markup=repeat_kb)
            else:
                await bot.send_photo(chat_id, photo=data,
                                     reply_markup=repeat_kb)

            credits_after = await get_credits(session)
            if credits_after is not None and credits_before is not None:
                used = max(credits_before - credits_after, 0)
                await bot.send_message(chat_id,
                    f"💳 {used:.2f} · {credits_after:.2f} left")
            elif credits_after is not None:
                await bot.send_message(chat_id,
                    f"💳 {credits_after:.2f} left")
            return True
    except Exception as e:
        log.exception("image edit failed")
        await bot.send_message(chat_id, f"Failed: {e}")
        return False
    finally:
        try:
            await status.delete()
        except Exception:
            pass


# ---------- IMAGE EDIT MODE ----------
MAX_EDIT_IMAGES = 5  # 1 target + up to 4 refs

async def img_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not image_authorised(update):
        return ConversationHandler.END
    context.user_data.clear()
    await clear_close(update, context)
    track(context, update.message.message_id)
    await say(update, context, "Send the photo.", CANCEL_KEYBOARD)
    return IMG_EDIT_PHOTO


async def img_edit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not image_authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    file_id = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        file_id = update.message.document.file_id
    if not file_id:
        await say(update, context, "Send a photo.", CANCEL_KEYBOARD)
        return IMG_EDIT_PHOTO
    tg_file = await context.bot.get_file(file_id)
    context.user_data["image_list"] = [bytes(await tg_file.download_as_bytearray())]
    await say(update, context, "➕📷 or ▶️", REFS_KEYBOARD)
    return IMG_EDIT_REFS


async def img_edit_refs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not image_authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)

    if update.message.text == BTN_BACK:
        context.user_data.pop("image_list", None)
        await say(update, context, "📷", CANCEL_KEYBOARD)
        return IMG_EDIT_PHOTO

    if update.message.text == BTN_NEXT:
        await say(update, context, "✏️", BACK_CANCEL_KEYBOARD)
        return IMG_EDIT_TEXT

    file_id = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        file_id = update.message.document.file_id

    if not file_id:
        await say(update, context, "📷 or ▶️", REFS_KEYBOARD)
        return IMG_EDIT_REFS

    img_list = context.user_data.get("image_list", [])
    if len(img_list) >= MAX_EDIT_IMAGES:
        await say(update, context, "🚫 max — ▶️", REFS_KEYBOARD)
        return IMG_EDIT_REFS

    tg_file = await context.bot.get_file(file_id)
    img_list.append(bytes(await tg_file.download_as_bytearray()))
    context.user_data["image_list"] = img_list
    refs = len(img_list) - 1
    slots = MAX_EDIT_IMAGES - len(img_list)
    if slots > 0:
        await say(update, context, f"✅ +{refs} — ➕📷 or ▶️", REFS_KEYBOARD)
        return IMG_EDIT_REFS
    else:
        await say(update, context, f"✅ +{refs} max — ✏️",
                  BACK_CANCEL_KEYBOARD)
        return IMG_EDIT_TEXT


async def img_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not image_authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    if update.message.text == BTN_BACK:
        await say(update, context, "➕📷 or ▶️", REFS_KEYBOARD)
        return IMG_EDIT_REFS
    text = (update.message.text or "").strip()
    if not text:
        await say(update, context, "Type the edit.", BACK_CANCEL_KEYBOARD)
        return IMG_EDIT_TEXT
    context.user_data["edit_prompt"] = text
    n = len(context.user_data.get("image_list", []))
    refs_note = f" +{n-1} ref" if n > 1 else ""
    await say(update, context, f"\"{text}\"{refs_note} — Go?",
              CONFIRM_KEYBOARD)
    return IMG_EDIT_CONFIRM


async def img_edit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not image_authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    if update.message.text == BTN_BACK:
        await say(update, context, "Edit prompt?", BACK_CANCEL_KEYBOARD)
        return IMG_EDIT_TEXT
    if update.message.text != BTN_GENERATE:
        await say(update, context, "Go, Back, or Cancel.", CONFIRM_KEYBOARD)
        return IMG_EDIT_CONFIRM

    prompt = context.user_data.get("edit_prompt")
    img_list = context.user_data.get("image_list")
    if not prompt or not img_list:
        await cleanup(update, context)
        await close_msg(update, context, "Lost data — restart.")
        return ConversationHandler.END

    await cleanup(update, context)
    asyncio.create_task(run_image_edit(
        context.bot, update.effective_chat.id,
        prompt=prompt, image_bytes_list=img_list,
        img_size=get_image_size(), img_quality=get_image_quality(),
    ))
    await close_msg(update, context, "⏳ Queued.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Entry points / menu
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not image_authorised(update):
        return
    kb = get_user_keyboard(update)
    if authorised(update):
        has_img = "yes" if load_default_image() else "no"
        await update.message.reply_text(
            "Pick a mode:\n"
            f"⚡ Quick — type only the dialogue (uses your default image; set: {has_img}).\n"
            "🎛️ Flexible — choose an image, then dialogue box or full prompt.\n"
            "🖌️ Edit — image-to-image editing (Seedream 5.0 Pro).\n"
            "⚙️ Settings — duration, quality, aspect, image, template.\n\n"
            f"Current: {get_quality()} · {get_duration()}s · {get_aspect()}. "
            "Step messages auto-clear; videos stay.",
            reply_markup=kb,
        )
    else:
        await update.message.reply_text(
            "🖌️ Edit — photo + prompt.",
            reply_markup=kb,
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

    await cleanup(update, context)
    prompt = load_template().format(dialogue=dialogue)
    # Snapshot settings and fire background task
    asyncio.create_task(run_generation(
        context.bot, update.effective_chat.id,
        prompt=prompt, caption=dialogue, image_url=image_url,
        duration=get_duration(), quality=get_quality(),
        aspect_ratio=get_aspect(), count=get_count(),
    ))
    await close_msg(update, context, "⏳ Queued. You can start another.")
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
    # Snapshot settings and fire background task
    asyncio.create_task(run_generation(
        context.bot, update.effective_chat.id,
        prompt=prompt, caption=caption, image_bytes=img_bytes,
        duration=get_duration(), quality=get_quality(),
        aspect_ratio=get_aspect(), count=get_count(),
    ))
    await close_msg(update, context, "⏳ Queued. You can start another.")
    return ConversationHandler.END


# ---------- SETTINGS ----------
def settings_overview_text() -> str:
    img = "set ✅" if load_default_image() else "not set ❌"
    tpl = "custom" if _load_state().get("prompt_template") else "default"
    dur = get_duration()
    qual = get_quality()
    cnt = get_count()
    cps = QUALITY_CREDITS_PER_SEC.get(qual, 0)
    est = dur * cps * cnt
    per_submit = f" (×{cnt} = {est:.0f} per submit)" if cnt > 1 else ""
    return (
        "⚙️ Settings — current setup\n"
        "── Video ──────────\n"
        f"⏱️ Length   :  {dur}s\n"
        f"🎚️ Quality  :  {qual}\n"
        f"📐 Aspect   :  {ASPECT_LABELS.get(get_aspect(), get_aspect())}\n"
        f"🔢 Count    :  {cnt} per submit\n"
        f"🖼️ Image    :  {img}\n"
        f"📝 Template :  {tpl}\n"
        f"≈ {dur*cps:.0f} credits per clip{per_submit}\n"
        "── Image Edit ─────\n"
        f"🖼️ Img Res  :  {get_image_quality()}\n"
        f"📐 Img Size :  {get_image_size()}\n"
        "──────────────\n"
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

    if update.message.text == BTN_SET_COUNT:
        await say(update, context,
                  f"How many clips per ✅ Go? (current: {get_count()})\n"
                  "2 means two independent variations — and 2× the credits.",
                  COUNT_KEYBOARD)
        return PICK_COUNT

    if update.message.text == BTN_SET_IMAGE:
        await say(update, context, "Send the photo to use as the default.",
                  BACK_CANCEL_KEYBOARD)
        return SET_IMAGE_WAIT

    if update.message.text == BTN_SET_IMG_QUALITY:
        await say(update, context,
                  f"Image edit resolution (current: {get_image_quality()}).\n"
                  "2K = higher quality, ~2× cost.",
                  IMG_QUALITY_KEYBOARD)
        return PICK_IMG_QUALITY

    if update.message.text == BTN_SET_IMG_SIZE:
        await say(update, context,
                  f"Image edit output size (current: {get_image_size()}).\n"
                  "auto = match input aspect.",
                  IMG_SIZE_KEYBOARD)
        return PICK_IMG_SIZE

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


async def pick_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    if update.message.text == BTN_BACK:
        return await show_settings(update, context)
    raw = (update.message.text or "").strip()
    if not raw.isdigit() or int(raw) not in COUNT_OPTIONS:
        await say(update, context, "Tap 1 or 2, or ⬅️ Back.", COUNT_KEYBOARD)
        return PICK_COUNT
    set_count(int(raw))
    return await show_settings(update, context)


async def pick_img_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    if update.message.text == BTN_BACK:
        return await show_settings(update, context)
    q = (update.message.text or "").strip()
    if q not in IMAGE_QUALITY_OPTIONS:
        await say(update, context, "Tap 1K or 2K, or ⬅️ Back.", IMG_QUALITY_KEYBOARD)
        return PICK_IMG_QUALITY
    set_image_quality(q)
    return await show_settings(update, context)


async def pick_img_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return ConversationHandler.END
    track(context, update.message.message_id)
    if update.message.text == BTN_BACK:
        return await show_settings(update, context)
    s = (update.message.text or "").strip()
    if s not in IMAGE_SIZE_OPTIONS:
        await say(update, context, "Tap one of the sizes, or ⬅️ Back.",
                  IMG_SIZE_KEYBOARD)
        return PICK_IMG_SIZE
    set_image_size(s)
    return await show_settings(update, context)


# ---------- shared cancel ----------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track(context, update.message.message_id)
    await cleanup(update, context)
    await close_msg(update, context, "Cancelled.")
    return ConversationHandler.END


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not image_authorised(update):
        return
    async with aiohttp.ClientSession() as session:
        bal = await get_credits(session)
    if bal is None:
        await update.message.reply_text("Couldn't fetch the balance right now.")
    else:
        await update.message.reply_text(f"💳 {bal:.2f} credits remaining.")


async def repeat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 🔁 inline button press — re-run the same image edit."""
    query = update.callback_query
    await query.answer()
    if not query.data or not query.data.startswith("rep:"):
        return
    rid = query.data[4:]
    params = _repeat_cache.get(rid)
    if not params:
        await query.edit_message_text("🔁 expired")
        return
    chat_id = params["chat_id"]
    asyncio.create_task(run_image_edit(
        context.bot, chat_id,
        prompt=params["prompt"],
        image_bytes_list=params["image_bytes_list"],
        img_size=params["img_size"],
        img_quality=params["img_quality"],
    ))


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    cancel_filter = filters.Regex(f"^{BTN_CANCEL}$")
    txt = filters.TEXT & ~filters.COMMAND & ~cancel_filter
    img = (filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_QUICK}$"), quick_start),
            MessageHandler(filters.Regex(f"^{BTN_FLEX}$"), flex_start),
            MessageHandler(filters.Regex(r"^🖌️ Edit$"), img_edit_start),
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
            PICK_COUNT: [MessageHandler(txt, pick_count)],
            PICK_IMG_QUALITY: [MessageHandler(txt, pick_img_quality)],
            PICK_IMG_SIZE: [MessageHandler(txt, pick_img_size)],
            IMG_EDIT_PHOTO: [MessageHandler(img | txt, img_edit_photo)],
            IMG_EDIT_REFS: [MessageHandler(img | txt, img_edit_refs)],
            IMG_EDIT_TEXT:  [MessageHandler(txt, img_edit_text)],
            IMG_EDIT_CONFIRM: [MessageHandler(txt, img_edit_confirm)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(cancel_filter, cancel),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CallbackQueryHandler(repeat_callback, pattern=r"^rep:"))
    app.add_handler(conv)
    log.info("Bot starting (long-polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
