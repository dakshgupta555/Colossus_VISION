"""Maintenance intelligence: one priority score for every item in the service queue.

score = 100 x weighted geometric mean of five factors in [floor, 1]:
    severity x duration x recurrence x impact x evidence      (weights and thresholds: data/farm_planner.json "maintenance")
Items come from the existing sources (the fleet service queue, SN-001 live alerts, the simulated farm); this module only scores,
categorises, stores status/history in SQLite and builds the action card. It never makes a diagnosis more certain than the source:
REAL (single-sensor) items are always phrased as "possible causes".
"""
import json
import math
import time

STATUSES = ["pending", "inspection", "resolved", "false_alarm"]
FAULT_CLASSES = ["soiling", "partial_shade", "series_resistance", "bypass_short"]
KINDS = FAULT_CLASSES + ["live_amber", "live_red", "zone_issue", "node_offline", "unexpected_readings"]


def init(con):
    con.execute("""CREATE TABLE IF NOT EXISTS maint_state (key TEXT PRIMARY KEY, status TEXT, occurrences INTEGER,
        first_ts REAL, last_seen REAL, note TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS maint_history (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, ts REAL, status TEXT, note TEXT)""")


def _hist(con, key, status, note):
    con.execute("INSERT INTO maint_history (key, ts, status, note) VALUES (?,?,?,?)", (key, time.time(), status, note))


def note_episode(con, key, note="episode"):
    """A new episode of the same issue (a recurrence)."""
    init(con)
    now = time.time()
    r = con.execute("SELECT occurrences FROM maint_state WHERE key=?", (key,)).fetchone()
    if r:
        con.execute("UPDATE maint_state SET occurrences=occurrences+1, status='pending', last_seen=? WHERE key=?", (now, key))
    else:
        con.execute("INSERT INTO maint_state VALUES (?,?,?,?,?,?)", (key, "pending", 1, now, now, ""))
    _hist(con, key, "pending", note)
    con.commit()


def set_status(con, key, status, note=""):
    if status not in STATUSES:
        raise ValueError("status")
    init(con)
    now = time.time()
    if con.execute("SELECT 1 FROM maint_state WHERE key=?", (key,)).fetchone():
        con.execute("UPDATE maint_state SET status=?, note=? WHERE key=?", (status, note, key))
    else:
        con.execute("INSERT INTO maint_state VALUES (?,?,?,?,?,?)", (key, status, 1, now, now, note))
    _hist(con, key, status, note)
    con.commit()


def history(con, key):
    init(con)
    return [dict(ts=ts, status=s, note=n) for ts, s, n in con.execute("SELECT ts, status, note FROM maint_history WHERE key=? ORDER BY id DESC", (key,))]


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def score(item, occurrences, cfg):
    m = cfg["maintenance"]
    w, floor = m["weights"], m["floor"]
    hours = max(0.0, item["minutes_ago"] / 60.0)
    f = dict(severity=_clamp(item["severity"]),
             duration=1 - 0.5 ** (hours / m["duration_half_life_h"]),
             recurrence=1 - 0.5 ** max(0, occurrences - 1),
             impact=_clamp((item.get("inr_day") or 0.0) / m["impact_inr_cap"]),
             evidence=_clamp(item["evidence_strength"]))
    fl = {k: max(floor, v) for k, v in f.items()}
    tot = sum(w.values())
    s = 100.0 * math.exp(sum(w[k] / tot * math.log(fl[k]) for k in w))
    c = m["categories"]
    cat = "critical" if s >= c["critical"] else "high" if s >= c["high"] else "medium" if s >= c["medium"] else "low"
    return round(s, 1), cat, {k: round(v, 2) for k, v in f.items()}


def card(item):
    """Action card as i18n keys: what happened, why it might have happened, what to do, why that action, evidence, follow-up."""
    kind, real = item["kind"], item["source"] == "REAL"
    cls = kind if kind in FAULT_CLASSES else None
    ev = []
    if item.get("loss_pct") is not None:
        ev.append(dict(key="maint.ev.loss", p=dict(loss=round(item["loss_pct"]), inr=round(item.get("inr_day") or 0, 1))))
    if kind in ("live_amber", "live_red") and item.get("avg_ma") is not None:
        ev.append(dict(key="maint.ev.current", p=dict(ma=round(item["avg_ma"], 2), drop=round(item.get("loss_pct") or 0))))
    if item.get("conf") is not None:
        ev.append(dict(key="maint.ev.conf", p=dict(x=round(item["conf"] * 100))))
    if kind == "zone_issue":
        ev.append(dict(key="maint.ev.neigh", p=dict(n=item.get("n_blocks", 0), nodes=item.get("n_nodes", 0))))
    if kind == "node_offline":
        ev.append(dict(key="maint.ev.offline", p=dict(min=round(item["minutes_ago"]))))
    ev.append(dict(key="maint.ev.age", p=dict(h=round(item["minutes_ago"] / 60.0, 1))))
    ev.append(dict(key="maint.ev.seen", p=dict(n=item.get("occurrences", 1))))
    if real:
        ev.append(dict(key="maint.ev.sensor", p={}))
    return dict(
        what=dict(key="maint.what." + kind, p=dict(node=item["node"], loss=round(item.get("loss_pct") or 0), n=item.get("n_blocks", 0))),
        why_head="maint.why.real" if real else "maint.why.sim",
        why=dict(key=("card.%s.cause" % cls) if cls else "maint.why." + kind),
        todo=dict(key=("act.%s" % cls) if cls else "maint.todo." + kind),
        why_action=dict(key="maint.wa." + kind),
        evidence=ev, follow=dict(key="maint.follow"))
