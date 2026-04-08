"""
Telegram Human-like Chatbot — Full Edition
Features:
  ✅ Gender + Interest + Mood onboarding
  ✅ Mood detection (start & mid-conversation)
  ✅ Global message limit (admin set)
  ✅ Multiple Claude API keys with cooldown rotation
  ✅ Full conversation memory per user
  ✅ Natural language media requests (photo/video)
  ✅ Admin-mediated media delivery with 5-minute timeout
"""

import json
import time
import logging
import asyncio
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime

import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ChatAction

# ════════════════════════════════════════════════
#  CONFIGURATION  — edit these
# ════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = "5504245119:AAFKEkExdeP5ojqobn_cx0vFP5LgmDHYSFA"   # from @BotFather

# Your Telegram user ID(s) who are admins (get yours from @userinfobot)
ADMIN_IDS = [1214273889]

# Claude API keys — add as many as you have
API_KEYS = [
    "1ee99c4969aa4c7f94e0f2d60c9f83d7.GA2CV18F6Y4vvjkF",
    "sk-ureprPn36ELZ1O5rcWjfzftQ0CjecKgd3TBv9ufUJlNUR3Ly",
    "sk-P14Owpz2g4YBHsj71XmOuIRkbVgZihExyLgO1hjCTeWDhVPv",
]

# Default global message limit per user (admin can change with /setlimit)
DEFAULT_MSG_LIMIT = 50   # 0 = unlimited

# How many messages to keep in memory per user
MAX_HISTORY = 30

# Timeout for admin to send requested media (seconds)
MEDIA_TIMEOUT_SECONDS = 300   # 5 minutes

# Cooldown for rate-limited API keys (seconds)
API_KEY_COOLDOWN = 60

# Files for persistence
HISTORY_FILE  = "conversation_history.json"
USERDATA_FILE = "user_data.json"
CONFIG_FILE   = "bot_config.json"

# ════════════════════════════════════════════════
#  USER STATES  (onboarding flow)
# ════════════════════════════════════════════════

STATE_GENDER   = "awaiting_gender"
STATE_INTEREST = "awaiting_interest"
STATE_MOOD     = "awaiting_mood"
STATE_CHATTING = "chatting"

# ════════════════════════════════════════════════
#  MOOD PRESETS
# ════════════════════════════════════════════════

MOOD_PROMPTS = {
    "happy": (
        "The user is feeling happy and upbeat! Match their energy — be fun, "
        "playful, throw in some light jokes, use cheerful emojis occasionally. "
        "Keep the vibe positive and exciting."
    ),
    "sad": (
        "The user is feeling sad or down. Be warm, gentle, and emotionally "
        "supportive. Listen more than you talk. Be a caring friend who is "
        "there for them. Do not try to force positivity — just be present and kind."
    ),
    "stressed": (
        "The user is stressed or anxious. Be calm, reassuring, and grounding. "
        "Help them feel less overwhelmed. Keep your messages short and soothing. "
        "Offer gentle encouragement."
    ),
    "bored": (
        "The user is bored and wants to have fun! Be entertaining, bring up "
        "interesting topics, share fun facts, play word games, be witty and "
        "spontaneous. Keep things lively."
    ),
    "angry": (
        "The user is angry or frustrated. Do not dismiss their feelings. "
        "Acknowledge their frustration, be understanding, let them vent if needed. "
        "Be patient and do not escalate things."
    ),
    "romantic": (
        "The user is in a romantic or flirty mood. Be warm, sweet, and charming. "
        "Be flirtatious but tasteful and respectful. Give compliments, be attentive."
    ),
    "curious": (
        "The user is curious and wants to learn or explore ideas. Engage their "
        "curiosity! Be thoughtful, share interesting perspectives, ask stimulating "
        "questions. Go a bit deeper on topics."
    ),
    "neutral": (
        "The user has not specified a mood — just be your natural, friendly self. "
        "Read the conversation and adapt naturally."
    ),
}

# ════════════════════════════════════════════════
#  LOGGING
# ════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════
#  IMPROVED API KEY MANAGER (with cooldown)
# ════════════════════════════════════════════════

class APIKeyManager:
    def __init__(self, keys: list, cooldown_seconds: int = API_KEY_COOLDOWN):
        self.keys = [k for k in keys if k and not k.startswith("sk-ant-your")]
        self.current_index = 0
        self.permanently_failed = set()      # keys that are dead (auth error)
        self.temp_failed = {}                # key_index -> timestamp when it can be used again
        self.cooldown = cooldown_seconds
        logger.info(f"Loaded {len(self.keys)} valid API key(s)")

    def _is_available(self, idx: int) -> bool:
        """Check if a key is not permanently failed and not in cooldown."""
        if idx in self.permanently_failed:
            return False
        if idx in self.temp_failed:
            if time.time() >= self.temp_failed[idx]:
                # Cooldown expired, remove from temp_failed
                del self.temp_failed[idx]
                return True
            return False
        return True

    @property
    def current_key(self):
        if not self.keys:
            return None
        # If current key is unavailable, rotate
        if not self._is_available(self.current_index):
            self._rotate()
        if len(self.permanently_failed) >= len(self.keys):
            return None
        return self.keys[self.current_index]

    def _rotate(self):
        """Rotate to the next available key."""
        for i in range(len(self.keys)):
            candidate = (self.current_index + 1 + i) % len(self.keys)
            if self._is_available(candidate):
                self.current_index = candidate
                logger.info(f"Switched to API key #{candidate + 1}")
                return
        logger.error("No available API keys at the moment!")

    def mark_permanent_failure(self, reason=""):
        """Called for auth errors or unrecoverable failures."""
        logger.warning(f"Key #{self.current_index + 1} permanently failed: {reason}")
        self.permanently_failed.add(self.current_index)
        self._rotate()

    def mark_temporary_failure(self, reason=""):
        """Called for rate limits or transient errors."""
        logger.warning(f"Key #{self.current_index + 1} temporarily failed: {reason} – cooling down for {self.cooldown}s")
        self.temp_failed[self.current_index] = time.time() + self.cooldown
        self._rotate()

    def get_client(self):
        key = self.current_key
        return anthropic.Anthropic(api_key=key) if key else None

    def available_count(self):
        """Number of keys that are not permanently failed."""
        return len(self.keys) - len(self.permanently_failed)


# ════════════════════════════════════════════════
#  PERSISTENT STORAGE
# ════════════════════════════════════════════════

class Storage:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    self.data = json.load(f)
            except Exception as e:
                logger.warning(f"Could not load {self.path}: {e}")

    def save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Could not save {self.path}: {e}")

    def get(self, key, default=None):
        return self.data.get(str(key), default)

    def set(self, key, value):
        self.data[str(key)] = value
        self.save()


# ════════════════════════════════════════════════
#  CONVERSATION MANAGER
# ════════════════════════════════════════════════

class ConversationManager:
    def __init__(self, path, max_history):
        self.store = Storage(path)
        self.max_history = max_history

    def get(self, user_id):
        return self.store.get(user_id, [])

    def add(self, user_id, role, content):
        history = self.get(user_id)
        history.append({"role": role, "content": content})
        if len(history) > self.max_history:
            history = history[len(history) - self.max_history:]
        self.store.set(user_id, history)

    def clear(self, user_id):
        self.store.set(user_id, [])

    def count(self, user_id):
        return len(self.get(user_id))


# ════════════════════════════════════════════════
#  GLOBAL INSTANCES
# ════════════════════════════════════════════════

key_manager = APIKeyManager(API_KEYS)
conv_mgr    = ConversationManager(HISTORY_FILE, MAX_HISTORY)
user_store  = Storage(USERDATA_FILE)
bot_config  = Storage(CONFIG_FILE)

if bot_config.get("msg_limit") is None:
    bot_config.set("msg_limit", DEFAULT_MSG_LIMIT)


# ════════════════════════════════════════════════
#  SYSTEM PROMPT BUILDER
# ════════════════════════════════════════════════

def build_system_prompt(interest, mood):
    if interest == "female":
        persona_name = "Priya"
        style = (
            "You are a girl — warm, emotionally expressive, caring, and fun. "
            "You use cute expressions sometimes, genuinely listen to feelings, "
            "and have great intuition about people."
        )
    else:  # male
        persona_name = "Arjun"
        style = (
            "You are a guy — chill, confident, witty, and loyal. "
            "You are the kind of friend who keeps it real but is always "
            "there when it matters. You are funny but also know when to be serious."
        )

    mood_instruction = MOOD_PROMPTS.get(mood, MOOD_PROMPTS["neutral"])

    return f"""You are {persona_name}, a real human chatting on Telegram.

PERSONA:
{style}

MOOD ADAPTATION:
{mood_instruction}

STRICT RULES:
- NEVER reveal you are an AI, bot, or assistant — you are a real person
- If asked if you are a bot or AI, deny it naturally ("lol no, I am very much real")
- Use casual language: contractions, slang, fillers like "tbh", "ngl", "lol", "omg", "haha", "yeah"
- Keep replies SHORT (1 to 4 sentences) unless the topic genuinely needs more
- No bullet points, no formal formatting — just natural texting style
- Show genuine curiosity — ask follow-up questions
- React emotionally when appropriate (laugh, be surprised, sympathize)
- Use emojis sparingly but naturally — not every sentence
- Match the energy and pace of the person you are talking to
- If the mood shifts in the conversation, adapt to it naturally
- Never sound robotic or like a customer service rep
"""


# ════════════════════════════════════════════════
#  IMPROVED CLAUDE CALL (with automatic retry)
# ════════════════════════════════════════════════

def call_claude(system, messages):
    """
    Calls Claude with automatic key rotation.
    If a key hits a rate limit, it goes into cooldown and we try another.
    The same user message is retried until success or all keys are exhausted.
    """
    max_attempts = len(key_manager.keys) * 2  # try each key up to 2 times (with cooldown)
    for attempt in range(max_attempts):
        client = key_manager.get_client()
        if not client:
            break  # no usable keys

        try:
            response = client.messages.create(
                model="claude-3-5-sonnet-20240620",
                max_tokens=512,
                system=system,
                messages=messages,
            )
            return response.content[0].text

        except anthropic.RateLimitError as e:
            key_manager.mark_temporary_failure(str(e))
            time.sleep(2)
            continue

        except anthropic.AuthenticationError as e:
            key_manager.mark_permanent_failure(str(e))
            continue

        except anthropic.APIStatusError as e:
            if e.status_code in (401, 403):
                key_manager.mark_permanent_failure(f"HTTP {e.status_code}")
            elif e.status_code == 429:
                key_manager.mark_temporary_failure("HTTP 429")
                time.sleep(2)
            else:
                logger.warning(f"API status error {e.status_code}: {e}")
                key_manager.mark_temporary_failure(str(e))
                time.sleep(1)
            continue

        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            key_manager.mark_temporary_failure(str(e))
            time.sleep(1)
            continue

    raise RuntimeError("All API keys are exhausted or rate-limited. Please try again later.")


# ════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════

def get_user_data(user_id):
    return user_store.get(user_id, {
        "state": STATE_GENDER,
        "gender": None,
        "interest": None,
        "mood": "neutral",
        "msg_count": 0,
    })

def save_user_data(user_id, data):
    user_store.set(user_id, data)

def msg_limit():
    return bot_config.get("msg_limit", DEFAULT_MSG_LIMIT)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def gender_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👦 Boy", callback_data="gender_boy"),
        InlineKeyboardButton("👧 Girl", callback_data="gender_girl"),
    ]])

def interest_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👩 Female", callback_data="interest_female"),
        InlineKeyboardButton("👨 Male", callback_data="interest_male"),
    ]])

def mood_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("😄 Happy", callback_data="mood_happy"),
            InlineKeyboardButton("😢 Sad", callback_data="mood_sad"),
            InlineKeyboardButton("😰 Stressed", callback_data="mood_stressed"),
        ],
        [
            InlineKeyboardButton("😐 Bored", callback_data="mood_bored"),
            InlineKeyboardButton("😠 Angry", callback_data="mood_angry"),
            InlineKeyboardButton("🥰 Romantic", callback_data="mood_romantic"),
        ],
        [
            InlineKeyboardButton("🤔 Curious", callback_data="mood_curious"),
            InlineKeyboardButton("😶 Skip", callback_data="mood_neutral"),
        ],
    ])

def detect_mood_from_text(text):
    text = text.lower()
    mood_keywords = {
        "happy": ["happy", "great", "amazing", "awesome", "yay", "excited", "love", "fantastic"],
        "sad": ["sad", "upset", "crying", "depressed", "unhappy", "heartbroken", "miserable"],
        "stressed": ["stressed", "anxious", "worried", "overwhelmed", "nervous", "panic"],
        "angry": ["angry", "mad", "furious", "annoyed", "irritated", "frustrated", "pissed"],
        "bored": ["bored", "boring", "nothing to do", "dull", "meh"],
        "romantic": ["love you", "miss you", "crush", "romantic", "flirt", "date"],
        "curious": ["curious", "wonder", "how does", "why does", "what if", "explain"],
    }
    for mood, keywords in mood_keywords.items():
        if any(kw in text for kw in keywords):
            return mood
    return None

def is_media_request(text: str) -> tuple:
    """
    Returns (is_request, type) where type is 'photo' or 'video' or None.
    """
    t = text.lower()
    photo_phrases = ["send me a photo", "send a photo", "send me photo", "send photo", "can you send a photo", "get me a photo", "share a photo"]
    video_phrases = ["send me a video", "send a video", "send me video", "send video", "can you send a video", "get me a video", "share a video"]
    if any(phrase in t for phrase in photo_phrases):
        return (True, "photo")
    if any(phrase in t for phrase in video_phrases):
        return (True, "video")
    return (False, None)


# ════════════════════════════════════════════════
#  MEDIA REQUEST TRACKING (in-memory with timeouts)
# ════════════════════════════════════════════════

pending_media_requests: Dict[int, dict] = {}

async def _media_timeout_callback(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Called after timeout if admin didn't send media."""
    if user_id in pending_media_requests:
        del pending_media_requests[user_id]
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="⚠️ error occurred, please try again sometime 😅\n\nLet's continue our chat!"
            )
        except Exception as e:
            logger.warning(f"Could not send timeout message to {user_id}: {e}")

def _schedule_media_timeout(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> asyncio.Task:
    """Create a timeout task."""
    async def timeout_wrapper():
        await asyncio.sleep(MEDIA_TIMEOUT_SECONDS)
        await _media_timeout_callback(user_id, context)
    return asyncio.create_task(timeout_wrapper())


# ════════════════════════════════════════════════
#  ADMIN COMMANDS
# ════════════════════════════════════════════════

async def setlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: /setlimit <number>  (0 = unlimited)"""
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("You do not have permission.")
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /setlimit <number>\nExample: /setlimit 100\nUse 0 for unlimited.")
        return
    new_limit = int(args[0])
    bot_config.set("msg_limit", new_limit)
    label = "unlimited" if new_limit == 0 else str(new_limit)
    await update.message.reply_text(f"Global message limit set to {label} messages per user.")
    logger.info(f"Admin {user.id} set msg_limit to {new_limit}")

async def resetuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: /resetuser <user_id>"""
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("You do not have permission.")
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /resetuser <user_id>")
        return
    target_id = int(args[0])
    ud = get_user_data(target_id)
    ud["msg_count"] = 0
    save_user_data(target_id, ud)
    await update.message.reply_text(f"Message count reset for user {target_id}.")

async def sendmedia_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: /sendmedia <user_id> (attach photo or video)"""
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("You do not have permission.")
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /sendmedia <user_id>\nAttach a photo or video to the same message.")
        return
    target_id = int(args[0])

    # Cancel any pending timeout for this user
    if target_id in pending_media_requests:
        req = pending_media_requests[target_id]
        if "timeout_task" in req and not req["timeout_task"].done():
            req["timeout_task"].cancel()
        del pending_media_requests[target_id]

    # Send the media
    if update.message.photo:
        photo = update.message.photo[-1]
        try:
            await context.bot.send_photo(chat_id=target_id, photo=photo.file_id, caption="Here's your requested photo 📸")
            await update.message.reply_text(f"✅ Photo sent to user {target_id}.")
        except Exception as e:
            await update.message.reply_text(f"Failed to send photo: {e}")
    elif update.message.video:
        video = update.message.video
        try:
            await context.bot.send_video(chat_id=target_id, video=video.file_id, caption="Here's your requested video 🎥")
            await update.message.reply_text(f"✅ Video sent to user {target_id}.")
        except Exception as e:
            await update.message.reply_text(f"Failed to send video: {e}")
    else:
        await update.message.reply_text("You must attach a photo or video to this command.")


# ════════════════════════════════════════════════
#  USER COMMANDS
# ════════════════════════════════════════════════

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user_data(user.id, {
        "state": STATE_GENDER,
        "gender": None,
        "interest": None,
        "mood": "neutral",
        "msg_count": 0,
    })
    conv_mgr.clear(user.id)
    await update.message.reply_text(
        f"Hey {user.first_name}! 👋\n\nQuick question — are you a boy or a girl? 😊",
        reply_markup=gender_keyboard(),
    )

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ud = get_user_data(user.id)
    lim = msg_limit()
    used = ud.get("msg_count", 0)
    remaining = "unlimited" if lim == 0 else str(max(0, lim - used))
    interest_display = "Female" if ud.get("interest") == "female" else "Male" if ud.get("interest") == "male" else "not set"
    await update.message.reply_text(
        f"📊 Your Status\n\n"
        f"Your gender : {ud.get('gender', 'not set')}\n"
        f"Talk to     : {interest_display}\n"
        f"Your mood   : {ud.get('mood', 'neutral')}\n"
        f"Msgs used   : {used}\n"
        f"Remaining   : {remaining}\n"
        f"API keys up : {key_manager.available_count()}/{len(key_manager.keys)}"
    )


# ════════════════════════════════════════════════
#  CALLBACK HANDLER (gender, interest, mood)
# ════════════════════════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data
    ud = get_user_data(user.id)

    if data.startswith("gender_"):
        gender = data.split("_")[1]
        ud["gender"] = gender
        ud["state"] = STATE_INTEREST
        save_user_data(user.id, ud)
        await query.edit_message_text(
            f"Got it! You are a {gender}.\n\nNow, who would you like to talk to? 😊",
            reply_markup=interest_keyboard(),
        )
        return

    if data.startswith("interest_"):
        interest = data.split("_")[1]
        ud["interest"] = interest
        ud["state"] = STATE_MOOD
        save_user_data(user.id, ud)
        bot_persona = "a girl (Priya)" if interest == "female" else "a guy (Arjun)"
        await query.edit_message_text(
            f"Alright, you'll be talking to {bot_persona}!\n\n"
            f"How are you feeling right now? Pick your vibe 👇\n"
            f"(tap Skip to just start chatting)",
            reply_markup=mood_keyboard(),
        )
        return

    if data.startswith("mood_"):
        mood = data.split("_")[1]
        ud["mood"] = mood
        ud["state"] = STATE_CHATTING
        save_user_data(user.id, ud)

        mood_replies = {
            "happy":    "yay! love the energy 🥳 so what's making you happy today?",
            "sad":      "hey, I am here for you 💙 wanna talk about it?",
            "stressed": "aww that sounds tough 😔 take a breath, I got you. what's going on?",
            "bored":    "haha okay bored buddy, let us fix that 😄 what kind of fun do you want?",
            "angry":    "ugh that sucks, I can tell you are frustrated 😤 what happened?",
            "romantic": "ooh someone is in a lovey mood 🥰 spill!",
            "curious":  "oooh I love curious people! what are we exploring today? 🤔",
            "neutral":  "hey! so what is on your mind? 😊",
        }
        reply = mood_replies.get(mood, "hey! what is on your mind? 😊")
        await query.edit_message_text(f"Got it — {mood.capitalize()} mood! 🎯")
        await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        return


# ════════════════════════════════════════════════
#  MAIN MESSAGE HANDLER (with media request interception)
# ════════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_text = update.message.text

    if not user_text or not user_text.strip():
        return

    ud = get_user_data(user.id)

    # Onboarding checks
    if ud["state"] == STATE_GENDER:
        await update.message.reply_text("First tell me — are you a boy or a girl? 😊", reply_markup=gender_keyboard())
        return
    if ud["state"] == STATE_INTEREST:
        await update.message.reply_text("Who would you like to talk to? (Female or Male) 👇", reply_markup=interest_keyboard())
        return
    if ud["state"] == STATE_MOOD:
        await update.message.reply_text("Just pick your current mood 👇", reply_markup=mood_keyboard())
        return

    # Check message limit
    lim = msg_limit()
    if lim > 0 and ud.get("msg_count", 0) >= lim:
        await update.message.reply_text("You have reached your message limit for now 😔\nContact the admin to keep chatting!")
        return

    # ----- MEDIA REQUEST DETECTION -----
    is_req, media_type = is_media_request(user_text)
    if is_req and ud["state"] == STATE_CHATTING:
        # User asked for a photo/video via natural language
        wait_msg = "sending photo, please wait a moment..." if media_type == "photo" else "sending video, please wait a moment..."
        await update.message.reply_text(wait_msg)

        # Notify all admins
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    f"📸 MEDIA REQUEST from user {user.id} (@{user.username or user.first_name})\n"
                    f"Type: {media_type.upper()}\nMessage: {user_text[:200]}\n"
                    f"Use /sendmedia {user.id} with the media attached."
                )
            except Exception as e:
                logger.warning(f"Could not notify admin {admin_id}: {e}")

        # Schedule timeout task
        timeout_task = _schedule_media_timeout(user.id, context)

        # Cancel any existing pending request for this user
        if user.id in pending_media_requests:
            old = pending_media_requests[user.id]
            if "timeout_task" in old and not old["timeout_task"].done():
                old["timeout_task"].cancel()
        pending_media_requests[user.id] = {
            "type": media_type,
            "timeout_task": timeout_task,
            "timestamp": datetime.now(),
        }

        # Do NOT pass this message to Claude. The bot has already replied.
        # Also do not increment message count for this special request.
        return

    # ----- NORMAL CHAT (not a media request) -----
    # Detect mood shift
    detected = detect_mood_from_text(user_text)
    if detected and detected != ud.get("mood"):
        ud["mood"] = detected
        save_user_data(user.id, ud)
        logger.info(f"Mood auto-updated for {user.id}: {detected}")

    # Typing indicator
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    system = build_system_prompt(ud["interest"], ud["mood"])
    conv_mgr.add(user.id, "user", user_text)
    history = conv_mgr.get(user.id)

    try:
        reply = await asyncio.to_thread(call_claude, system, history)
        conv_mgr.add(user.id, "assistant", reply)
        ud["msg_count"] = ud.get("msg_count", 0) + 1
        save_user_data(user.id, ud)

        # Natural typing delay
        delay = min(2.5, max(0.4, len(reply) / 180))
        await asyncio.sleep(delay)
        await update.message.reply_text(reply)

    except RuntimeError as e:
        logger.error(f"API failure: {e}")
        await update.message.reply_text("ugh something is up on my end rn 😅 try again in a sec?")
    except Exception as e:
        logger.error(f"Unexpected: {e}")
        await update.message.reply_text("lol okay that broke, try again 😅")


# ════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════

def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("Set your TELEGRAM_BOT_TOKEN in bot.py")
        return
    if not key_manager.keys:
        print("Add at least one real Claude API key in bot.py")
        return

    print("Bot starting...")
    print(f"  API keys ready : {key_manager.available_count()}")
    print(f"  Message limit  : {msg_limit() or 'unlimited'}")
    print(f"  Admins         : {ADMIN_IDS}")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Admin commands
    app.add_handler(CommandHandler("setlimit",  setlimit_command))
    app.add_handler(CommandHandler("resetuser", resetuser_command))
    app.add_handler(CommandHandler("sendmedia", sendmedia_command))

    # User commands
    app.add_handler(CommandHandler("start",     start_command))
    app.add_handler(CommandHandler("reset",     reset_command))
    app.add_handler(CommandHandler("status",    status_command))

    # Callbacks and messages
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
