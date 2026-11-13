"""One-time Telegram setup.

1. In Telegram, message @BotFather -> /newbot -> copy the token.
2. Send any message (e.g. "hi") to your new bot from your phone.
3. Run:  python setup_telegram.py <TOKEN>
It finds your chat id, saves telegram_config.json and sends a test message.
"""
import json
import sys

import requests

from sn.alerts import CFG_PATH, send

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)
token = sys.argv[1].strip()
r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10).json()
chats = {u["message"]["chat"]["id"] for u in r.get("result", []) if "message" in u}
if not chats:
    print("No messages found. Send 'hi' to your bot from your phone first, then run this again.")
    sys.exit(1)
cfg = dict(token=token, chat_id=sorted(chats)[-1])
with open(CFG_PATH, "w") as fh:
    json.dump(cfg, fh)
print("Saved", CFG_PATH, "chat_id =", cfg["chat_id"])
print("Test message sent:", send("SuryaNidaan is connected. Fault alerts will arrive here."))
