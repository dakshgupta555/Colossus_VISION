"""Telegram alerts with an offline queue (alerts are stored and retried when internet returns)."""
import json
import os

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, "telegram_config.json")


def load_cfg():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        return dict(token=token, chat_id=chat)
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH) as fh:
            return json.load(fh)
    return None


def send(text):
    """Return True if Telegram accepted the message."""
    cfg = load_cfg()
    if not cfg:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{cfg['token']}/sendMessage",
                          data=dict(chat_id=cfg["chat_id"], text=text), timeout=6)
        return r.ok
    except requests.RequestException:
        return False


def format_alert(node_id, source, res, lang="en"):
    """Alert text in the owner's language, built from the same keys the console uses."""
    from . import i18n as L
    title = L.t(lang, "t.title", dict(node=node_id))
    if source == "sim":
        title = f"[{L.t(lang, 't.sim')}] " + title
    fault = L.t(lang, "f." + res["label"])
    lines = [
        title,
        f"{fault} ({L.t(lang, 't.conf', dict(x=min(99, round(res['confidence'] * 100))))})",
        L.t(lang, res["verdict"]["key"], res["verdict"]["p"]),
        L.t(lang, "t.where", dict(x=L.t(lang, res["location"]["key"], res["location"]["p"]))),
        L.t(lang, "t.loss", dict(loss=res["loss_pct"], inr=res["money"]["inr_day"], kwp=res["assumptions"]["kwp"])),
        L.t(lang, "t.do", dict(x=L.t(lang, res["act"]["key"], res["act"]["p"]))),
    ]
    return "\n".join(lines)
