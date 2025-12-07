import json
import telebot
import subprocess
import os
import uuid
import re
import time
import psutil
from datetime import datetime, timedelta

# ================= CONFIG =================

TOKEN = "8396930272:AAGYfHsNbjDreqrEJUiweGEBFfXcKcMTMzY"
ADMIN_ID = 1214273889  

REELS_DIR = "reels"
FILE_DB = "file_ids.json"

bot = telebot.TeleBot(TOKEN)
START_TIME = time.time()

if not os.path.exists(REELS_DIR):
    os.makedirs(REELS_DIR)

# ================= ADMIN SYSTEM =================

def is_admin(message):
    return message.from_user.id == ADMIN_ID

# ================= CACHE SYSTEM =================

def load_file_ids():
    if os.path.exists(FILE_DB):
        try:
            with open(FILE_DB, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_file_ids(data):
    with open(FILE_DB, "w") as f:
        json.dump(data, f)

# ================= REEL ID =================

def extract_reel_id(url):
    match = re.search(r"instagram\.com/reels?/([A-Za-z0-9_-]+)/?", url)
    return match.group(1) if match else None

# ================= PROGRESS BAR =================

progress_msg_id = None
progress_chat_id = None
last_update_time = 0

def update_progress(percent, speed, eta):
    global last_update_time
    now = time.time()
    if now - last_update_time > 1.2:
        try:
            bot.edit_message_text(
                f"⬇️ Downloading...\n\n"
                f"📊 Progress: {percent}\n"
                f"🚀 Speed: {speed}\n"
                f"⏳ ETA: {eta}",
                chat_id=progress_chat_id,
                message_id=progress_msg_id
            )
            last_update_time = now
        except:
            pass

def parse_progress(line):
    percent = re.search(r"(\d{1,3}\.\d+)%", line)
    speed = re.search(r"at\s+([\d\.]+[KMG]iB/s)", line)
    eta = re.search(r"ETA\s+(\d+:\d+)", line)

    return (
        percent.group(1) + "%" if percent else "0%",
        speed.group(1) if speed else "0 B/s",
        eta.group(1) if eta else "..."
    )

# ================= COMMANDS =================

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🔥 Send Instagram REEL link (multiple supported)")

@bot.message_handler(commands=['myid'])
def myid(message):
    bot.reply_to(message, f"🆔 Your ID: `{message.from_user.id}`", parse_mode="Markdown")

@bot.message_handler(commands=['admins'])
def admins(message):
    bot.reply_to(message, f"👑 Current Admin ID:\n`{ADMIN_ID}`", parse_mode="Markdown")

# ✅ ADMIN ONLY
@bot.message_handler(commands=['ping'])
def ping(message):
    if not is_admin(message):
        return

    uptime = round((time.time() - START_TIME) / 60, 2)
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent

    msg = (
        "🏓 *PONG!*\n\n"
        f"⚡ Bot Speed: {round(1000 / (uptime * 60 + 1), 2)} ms\n"
        f"⏳ Uptime: {uptime} minutes\n"
        f"🧠 CPU Usage: {cpu}%\n"
        f"💾 RAM Usage: {ram}%\n"
        f"📁 Cached Reels: {len(load_file_ids())}"
    )

    bot.reply_to(message, msg, parse_mode="Markdown")

# ✅ ADMIN ONLY
@bot.message_handler(commands=['cache'])
def cache_size(message):
    if not is_admin(message):
        return
    total = sum(os.path.getsize(os.path.join(REELS_DIR, f)) for f in os.listdir(REELS_DIR))
    mb = round(total / 1024 / 1024, 2)
    bot.reply_to(message, f"📦 Cache Size: {mb} MB")

# ✅ ADMIN ONLY
@bot.message_handler(commands=['clearcache'])
def clear_cache(message):
    if not is_admin(message):
        return

    for f in os.listdir(REELS_DIR):
        os.remove(os.path.join(REELS_DIR, f))

    save_file_ids({})
    bot.reply_to(message, "🗑 Cache Cleared Successfully")

# ✅ ADMIN ONLY
@bot.message_handler(commands=['autoclean'])
def auto_clean(message):
    if not is_admin(message):
        return

    days = 7
    now = datetime.now()
    deleted = 0

    for f in os.listdir(REELS_DIR):
        path = os.path.join(REELS_DIR, f)
        if os.path.isfile(path):
            file_time = datetime.fromtimestamp(os.path.getmtime(path))
            if now - file_time > timedelta(days=days):
                os.remove(path)
                deleted += 1

    bot.reply_to(message, f"✅ AutoClean Done\n🗑 Deleted: {deleted} files older than {days} days")

# ================= MULTI REEL HANDLER (PUBLIC) =================

@bot.message_handler(func=lambda message: True)
def download_reels(message):

    urls = re.findall(r"https?://[^\s]+", message.text)
    if not urls:
        return

    for url in urls:

        reel_id = extract_reel_id(url)
        if reel_id is None:
            continue

        saved_name = f"{reel_id}.mp4"
        saved_path = os.path.join(REELS_DIR, saved_name)
        file_ids = load_file_ids()

        # ⚡ TELEGRAM CACHE
        if reel_id in file_ids:
            bot.send_message(message.chat.id, "⚡ Sent instantly from cache!")
            bot.send_video(message.chat.id, file_ids[reel_id])
            continue

        # 📂 DISK CACHE
        if os.path.exists(saved_path):
            bot.send_message(message.chat.id, "📂 Sending saved reel…")
            with open(saved_path, "rb") as video:
                sent = bot.send_video(message.chat.id, video, timeout=200)

            file_ids[reel_id] = sent.video.file_id
            save_file_ids(file_ids)
            continue

        # ⬇️ DOWNLOAD
        progress_msg = bot.send_message(message.chat.id, "⬇️ Starting download...")
        global progress_msg_id, progress_chat_id, last_update_time
        progress_msg_id = progress_msg.message_id
        progress_chat_id = message.chat.id
        last_update_time = 0

        temp_name = f"temp_{uuid.uuid4().hex}.mp4"
        venv_python = "python"


        cmd = [
            venv_python, "-m", "yt_dlp",
            "-f", "mp4",
            "--no-playlist",
            "--socket-timeout", "25",
            "--retries", "5",
            "--newline",
            "-o", temp_name,
            url
        ]

        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            for line in process.stdout:
                percent, speed, eta = parse_progress(line)
                update_progress(percent, speed, eta)

            process.wait()

        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Download failed:\n{str(e)}")
            continue

        bot.send_message(message.chat.id, "📤 Uploading to Telegram...")

        try:
            with open(temp_name, "rb") as video:
                sent = bot.send_video(message.chat.id, video, timeout=200)

            file_ids = load_file_ids()
            file_ids[reel_id] = sent.video.file_id
            save_file_ids(file_ids)

            os.rename(temp_name, saved_path)
            bot.send_message(message.chat.id, "✅ Reel saved & cached")

        except:
            os.rename(temp_name, saved_path)
            bot.send_message(message.chat.id, "⚠️ Upload failed but reel is saved locally")

# ================= RUN BOT =================

bot.polling(non_stop=True)

