import requests
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = "5504245119:AAFKEkExdeP5ojqobn_cx0vFP5LgmDHYSFA"
OPENAI_API_KEY = "sk-P14Owpz2g4YBHsj71XmOuIRkbVgZihExyLgO1hjCTeWDhVPv"

# Send message back to Telegram
def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": chat_id,
        "text": text
    })

# Ask GPT
def ask_gpt(user_text):
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}"
    }
    json_data = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "You are a friendly chat bot."},
            {"role": "user", "content": user_text}
        ]
    }
    res = requests.post(url, headers=headers, json=json_data)
    return res.json()["choices"][0]["message"]["content"]

# Telegram webhook
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        user_text = data["message"].get("text", "")

        reply = ask_gpt(user_text)
        send_message(chat_id, reply)

    return "ok"

app.run(port=5000)
