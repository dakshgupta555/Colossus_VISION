"""Simple REAL live mode: panel current -> green / amber / red. No baseline, no jumper.

All numbers live in data/live_thresholds.json (see that file). The state a person sees only changes after the
new state has held for hold_s seconds, so a reading near a threshold does not flicker.
"""
import json
import os
import threading

from . import calib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.environ.get("SN_LIVE_CFG") or os.path.join(ROOT, "data", "live_thresholds.json")
DEFAULT = dict(mode="simple", green_ma=0.35, amber_ma=0.20, full_ma=0.5, avg_s=2.0, hold_s=1.5, alert_gap_s=30)
STATE_TO_TWIN = {"green": "healthy", "amber": "attention", "red": "critical"}   # names the twin and the page already style


def load_cfg():
    try:
        return {**DEFAULT, **{k: v for k, v in json.load(open(PATH)).items() if not k.startswith("_")}}
    except (OSError, ValueError):
        return dict(DEFAULT)


def window_avg(readings, now, secs, cal=None):
    """Mean calibrated current (mA) over the last `secs` seconds, and how many readings it used."""
    cal = cal or calib.load()
    vals = [calib.apply(r["ma"], cal) for r in readings if now - r["ts"] <= secs]
    return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)


def classify(ma, cfg):
    if ma is None:
        return None
    return "green" if ma >= cfg["green_ma"] else "amber" if ma >= cfg["amber_ma"] else "red"


def drop_pct(ma, cfg):
    """Approximate drop against full torch light: 1 - current/full_ma, clamped to 0-100%."""
    if ma is None:
        return None
    return round(max(0.0, min(1.0, 1.0 - ma / cfg["full_ma"])) * 100.0, 1)


def light_pct(ma, cfg):
    return None if ma is None else round(max(0.0, ma / cfg["full_ma"]) * 100.0, 1)


class Tracker:
    """Shows a new state only after it has held for hold_s seconds."""

    def __init__(self):
        self.committed, self.cand, self.since = None, None, 0.0
        self._lock = threading.Lock()

    def update(self, raw, now, hold_s):
        """-> (committed, changed, previous). The first sample is committed at once."""
        with self._lock:
            prev = self.committed
            if raw is None:
                return self.committed, False, prev
            if self.committed is None:
                self.committed = raw
                return raw, True, None
            if raw == self.committed:
                self.cand = None
                return self.committed, False, prev
            if self.cand != raw:
                self.cand, self.since = raw, now
            if now - self.since >= hold_s:
                self.committed, self.cand = raw, None
                return raw, True, prev
            return self.committed, False, prev

    def snapshot(self):
        with self._lock:
            return dict(committed=self.committed, cand=self.cand)
