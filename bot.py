"""
Telegram Human-like Chatbot — Advanced Edition
Features:
  ✅ Gender selection on start (boy → girl bot, girl → boy bot)
  ✅ Mood detection after gender — adapts tone to user's mood
  ✅ Mood detected mid-conversation too — always stays in sync
  ✅ Global message limit set by admin
  ✅ Multiple Claude API keys with auto-rotation
  ✅ Full conversation memory per user (persisted to disk)
"""

import json
import time
import logging
import asyncio
from pathlib import Path
from typing import Optional

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

TELEGRAM_BOT_TOKEN = "1ee99c4969aa4c7f94e0f2d60c9f83d7.GA2CV18F6Y4vvjkF"   # from @BotFather

# Your Telegram user ID(s) who are admins (get yours from @userinfobot)
ADMIN_IDS = [ 1214273889]

# Claude API keys — add as many as you have
API_KEYS = [
    "sk-P14Owpz2g4YBHsj71XmOuIRkbVgZihExyLgO1hjCTeWDhVPv",
    "sk-1234567890abcdef1234567890abcdef12345678",
    "sk-P14Owpz2g4YBHsj71XmOuIRkbVgZihExyLgO1hjCTeWDhVPv",
]

# Default global message limit per user (admin can change with /setlimit)
DEFAULT_MSG_LIMIT = 50   # 0 = unlimited

# How many messages to keep in memory per user
MAX_HISTORY = 30

# Files for persistence
HISTORY_FILE  = "conversation_history.json"
USERDATA_FILE = "user_data.json"
CONFIG_FILE   = "bot_config.json"

# ════════════════════════════════════════════════
#  USER STATES  (onboarding flow)
# ════════════════════════════════════════════════

STATE_GENDER   = "awaiting_gender"
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
#  API KEY MANAGER
# ════════════════════════════════════════════════

class APIKeyManager:
    def __init__(self, keys: list):
        self.keys = [k for k in keys if k and not k.startswith("sk-ant-your")]
        self.current_index = 0
        self.failed = set()
        logger.info(f"Loaded {len(self.keys)} valid API key(s)")

    @property
    def current_key(self):
        if not self.keys:
            return None
        if self.current_index in self.failed:
            self._rotate()
        if len(self.failed) >= len(self.keys):
            return None
        return self.keys[self.current_index]

    def _rotate(self):
        for i in range(len(self.keys)):
            candidate = (self.current_index + 1 + i) % len(self.keys)
            if candidate not in self.failed:
                self.current_index = candidate
                logger.info(f"Switched to API key #{candidate + 1}")
                return
        logger.error("All API keys exhausted!")

    def mark_failed(self, reason=""):
        logger.warning(f"Key #{self.current_index + 1} failed: {reason}")
        self.failed.add(self.current_index)
        self._rotate()

    def get_client(self):
        key = self.current_key
        return anthropic.Anthropic(api_key=key) if key else None

    def available_count(self):
        return len(self.keys) - len(self.failed)


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

def build_system_prompt(user_gender, mood):
    if user_gender == "boy":
        persona_name = "Priya"
        style = (
            "You are a girl — warm, emotionally expressive, caring, and fun. "
            "You use cute expressions sometimes, genuinely listen to feelings, "
            "and have great intuition about people."
        )
    else:
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
#  CLAUDE API CALL
# ════════════════════════════════════════════════

def call_claude(system, messages):
    max_attempts = max(len(API_KEYS), 1)
    for attempt in range(max_attempts):
        client = key_manager.get_client()
        if not client:
            raise RuntimeError("All API keys exhausted.")
        try:
            response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=512,
                system=system,
                messages=messages,
            )
            return response.content[0].text

        except anthropic.AuthenticationError as e:
            key_manager.mark_failed(f"Auth error: {e}")
        except anthropic.RateLimitError as e:
            key_manager.mark_failed(f"Rate limit: {e}")
        except anthropic.APIStatusError as e:
            if e.status_code in (401, 403):
                key_manager.mark_failed(f"HTTP {e.status_code}")
            else:
                logger.warning(f"API error: {e}")
                time.sleep(1)
                if attempt >= 1:
                    key_manager.mark_failed(f"Repeated error: {e}")
        except Exception as e:
            logger.error(f"Unexpected: {e}")
            raise

    raise RuntimeError("Failed after trying all API keys.")


# ════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════

def get_user_data(user_id):
    return user_store.get(user_id, {
        "state":     STATE_GENDER,
        "gender":    None,
        "mood":      "neutral",
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
        InlineKeyboardButton("👦 Boy",  callback_data="gender_boy"),
        InlineKeyboardButton("👧 Girl", callback_data="gender_girl"),
    ]])

def mood_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("😄 Happy",    callback_data="mood_happy"),
            InlineKeyboardButton("😢 Sad",      callback_data="mood_sad"),
            InlineKeyboardButton("😰 Stressed", callback_data="mood_stressed"),
        ],
        [
            InlineKeyboardButton("😐 Bored",    callback_data="mood_bored"),
            InlineKeyboardButton("😠 Angry",    callback_data="mood_angry"),
            InlineKeyboardButton("🥰 Romantic", callback_data="mood_romantic"),
        ],
        [
            InlineKeyboardButton("🤔 Curious",  callback_data="mood_curious"),
            InlineKeyboardButton("😶 Skip",     callback_data="mood_neutral"),
        ],
    ])

def detect_mood_from_text(text):
    text = text.lower()
    mood_keywords = {
        "happy":    ["happy", "great", "amazing", "awesome", "yay", "excited", "love", "fantastic", "wonderful", "overjoyed"],
        "sad":      ["sad", "upset", "crying", "depressed", "unhappy", "heartbroken", "miserable", "lonely", "hopeless", "down"],
        "stressed": ["stressed", "anxious", "worried", "overwhelmed", "nervous", "panic", "tense", "anxiety"],
        "angry":    ["angry", "mad", "furious", "annoyed", "irritated", "frustrated", "pissed", "hate"],
        "bored":    ["bored", "boring", "nothing to do", "dull", "meh"],
        "romantic": ["love you", "miss you", "crush", "romantic", "flirt", "date", "feelings for"],
        "curious":  ["curious", "wonder", "how does", "why does", "what if", "explain", "tell me about"],
    }
    for mood, keywords in mood_keywords.items():
        if any(kw in text for kw in keywords):
            return mood
    return None


# ════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ════════════════════════════════════════════════

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user_data(user.id, {
        "state":     STATE_GENDER,
        "gender":    None,
        "mood":      "neutral",
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
    ud   = get_user_data(user.id)
    lim  = msg_limit()
    used = ud.get("msg_count", 0)
    remaining = "unlimited" if lim == 0 else str(max(0, lim - used))

    gender = ud.get("gender")
    if gender == "boy":
        persona = "Priya (girl) 👧"
    elif gender == "girl":
        persona = "Arjun (boy) 👦"
    else:
        persona = "not set yet"

    await update.message.reply_text(
        f"📊 Your Status\n\n"
        f"Chat persona : {persona}\n"
        f"Your mood    : {ud.get('mood', 'neutral')}\n"
        f"Messages used: {used}\n"
        f"Remaining    : {remaining}\n"
        f"API keys up  : {key_manager.available_count()}/{len(key_manager.keys)}"
    )


async def setlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: /setlimit <number>  (0 = unlimited)"""
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("You do not have permission to use this command.")
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
        await update.message.reply_text("You do not have permission to use this command.")
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


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data
    ud   = get_user_data(user.id)

    # Gender selection
    if data.startswith("gender_"):
        gender = data.split("_")[1]
        ud["gender"] = gender
        ud["state"]  = STATE_MOOD
        persona_name = "Priya" if gender == "boy" else "Arjun"
        save_user_data(user.id, ud)
        await query.edit_message_text(
            f"Nice! I am {persona_name} 😊\n\n"
            f"How are you feeling right now? Pick your vibe 👇\n"
            f"(tap Skip to just start chatting)",
            reply_markup=mood_keyboard(),
        )
        return

    # Mood selection
    if data.startswith("mood_"):
        mood = data.split("_")[1]
        ud["mood"]  = mood
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user      = update.effective_user
    user_text = update.message.text

    if not user_text or not user_text.strip():
        return

    ud = get_user_data(user.id)

    # Still in onboarding?
    if ud["state"] == STATE_GENDER:
        await update.message.reply_text(
            "Hey! First tell me — are you a boy or a girl? 😊",
            reply_markup=gender_keyboard(),
        )
        return

    if ud["state"] == STATE_MOOD:
        await update.message.reply_text(
            "Almost there! Just pick your current mood 👇",
            reply_markup=mood_keyboard(),
        )
        return

    # Check message limit
    lim = msg_limit()
    if lim > 0 and ud.get("msg_count", 0) >= lim:
        await update.message.reply_text(
            "you have reached your message limit for now 😔\n"
            "contact the admin to keep chatting!"
        )
        return

    # Detect mood shift from text
    detected = detect_mood_from_text(user_text)
    if detected and detected != ud.get("mood"):
        ud["mood"] = detected
        logger.info(f"Mood auto-updated for {user.id}: {detected}")

    # Typing indicator
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING,
    )

    system  = build_system_prompt(ud["gender"], ud["mood"])
    conv_mgr.add(user.id, "user", user_text)
    history = conv_mgr.get(user.id)

    try:
        reply = await asyncio.to_thread(call_claude, system, history)
        conv_mgr.add(user.id, "assistant", reply)
        ud["msg_count"] = ud.get("msg_count", 0) + 1
        save_user_data(user.id, ud)

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
    app.add_handler(CommandHandler("start",     start_command))
    app.add_handler(CommandHandler("reset",     reset_command))
    app.add_handler(CommandHandler("status",    status_command))
    app.add_handler(CommandHandler("setlimit",  setlimit_command))
    app.add_handler(CommandHandler("resetuser", resetuser_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
