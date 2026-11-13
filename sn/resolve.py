"""Fault resolution: extends the EXISTING tickets table (no parallel system).

tickets gains: fault_key, stage, contact_id, report_id, notes, updated_ts, verify_json, verified.
New tables: contacts (service directory, starts EMPTY), reports (what was generated), ticket_history.
Stages: detected -> report_generated -> contacted -> inspection -> awaiting_verification -> resolved (+ reopened).
Nothing here sends anything: drafts are text and mailto:/tel: links the person opens themselves.
"""
import hashlib
import json
import time
import urllib.parse

from . import i18n

STAGES = ["detected", "report_generated", "contacted", "inspection", "awaiting_verification", "resolved", "reopened"]
STAGE_TO_MAINT = {"detected": "pending", "report_generated": "pending", "contacted": "pending", "inspection": "inspection",
                  "awaiting_verification": "inspection", "resolved": "resolved", "reopened": "pending"}
CATEGORIES = ["technician", "cleaning", "manufacturer", "site_manager", "authority"]
CONTACT_FIELDS = ["name", "category", "phone", "email", "area", "website", "hours", "notes", "preferred"]
NEW_TICKET_COLS = dict(fault_key="TEXT", stage="TEXT", contact_id="INTEGER", report_id="TEXT", notes="TEXT", updated_ts="REAL",
                       verify_json="TEXT", verified="INTEGER", snapshot="TEXT")


def init(con):
    con.execute("""CREATE TABLE IF NOT EXISTS contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, category TEXT, phone TEXT, email TEXT,
        area TEXT, website TEXT, hours TEXT, notes TEXT, preferred TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, ts REAL, fault_id TEXT, fault_key TEXT, ticket_id INTEGER, recipient TEXT,
        lang TEXT, snapshot TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS ticket_history (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, ts REAL, stage TEXT, note TEXT,
        contact_id INTEGER, report_id TEXT)""")
    have = {r[1] for r in con.execute("PRAGMA table_info(tickets)")}
    for col, ty in NEW_TICKET_COLS.items():
        if col not in have:
            con.execute(f"ALTER TABLE tickets ADD COLUMN {col} {ty}")


def fault_id(key):
    return "F-" + hashlib.sha1(key.encode()).hexdigest()[:6].upper()


# ---------------------------------------------------------------- service directory
def _clean(d):
    c = {k: (str(d.get(k)).strip() if d.get(k) not in (None, "") else "") for k in CONTACT_FIELDS}
    if c["category"] not in CATEGORIES:
        raise ValueError("category")
    if not c["name"]:
        raise ValueError("name")
    c["preferred"] = c["preferred"] if c["preferred"] in ("phone", "email", "either") else "either"
    return c


def contact_row(r):
    return dict(zip(["id"] + CONTACT_FIELDS, r))


def contacts(con):
    return [contact_row(r) for r in con.execute("SELECT id, " + ",".join(CONTACT_FIELDS) + " FROM contacts ORDER BY category, name")]


def contact_add(con, d):
    c = _clean(d)
    cur = con.execute("INSERT INTO contacts (" + ",".join(CONTACT_FIELDS) + ") VALUES (" + ",".join("?" * len(CONTACT_FIELDS)) + ")", [c[k] for k in CONTACT_FIELDS])
    con.commit()
    return cur.lastrowid


def contact_update(con, cid, d):
    c = _clean(d)
    con.execute("UPDATE contacts SET " + ",".join(f"{k}=?" for k in CONTACT_FIELDS) + " WHERE id=?", [c[k] for k in CONTACT_FIELDS] + [cid])
    con.commit()


def contact_delete(con, cid):
    con.execute("DELETE FROM contacts WHERE id=?", (cid,))
    con.commit()


def match(con, category, area):
    """Contacts for a service category, ranked by category (exact only) then by service area match."""
    area = (area or "").lower()

    def rank(c):
        a = (c["area"] or "").lower()
        return 0 if a and area and (a in area or area in a) else 1 if not a else 2
    return sorted([c for c in contacts(con) if c["category"] == category], key=lambda c: (rank(c), c["name"].lower()))


# ---------------------------------------------------------------- tickets
def ticket_get(con, key=None, tid=None):
    sql = "SELECT id, ts, site, label, status, fault_key, stage, contact_id, report_id, notes, updated_ts, verify_json, verified FROM tickets WHERE "
    r = con.execute(sql + ("fault_key=? ORDER BY id DESC LIMIT 1" if key else "id=?"), (key if key else tid,)).fetchone()
    if not r:
        return None
    t = dict(zip(["id", "ts", "site", "label", "status", "fault_key", "stage", "contact_id", "report_id", "notes", "updated_ts", "verify", "verified"], r))
    t["verify"] = json.loads(t["verify"]) if t["verify"] else None
    t["code"] = "T-%04d" % t["id"]
    return t


def ticket_create(con, key, site, label):
    t = ticket_get(con, key=key)
    if t and t["stage"] != "resolved":
        return t
    now = time.time()
    cur = con.execute("INSERT INTO tickets (ts, site, label, status, fault_key, stage, updated_ts) VALUES (?,?,?,?,?,?,?)",
                      (now, site, label, "open", key, "detected", now))
    con.execute("INSERT INTO ticket_history (ticket_id, ts, stage, note) VALUES (?,?,?,?)", (cur.lastrowid, now, "detected", "ticket created"))
    con.commit()
    return ticket_get(con, tid=cur.lastrowid)


def set_stage(con, tid, stage, note="", contact_id=None, report_id=None, verified=None, verify=None):
    if stage not in STAGES:
        raise ValueError("stage")
    now = time.time()
    sets, args = ["stage=?", "status=?", "updated_ts=?"], [stage, "closed" if stage == "resolved" else "open", now]
    if note:
        sets.append("notes=?"); args.append(note)
    if contact_id is not None:
        sets.append("contact_id=?"); args.append(contact_id)
    if report_id:
        sets.append("report_id=?"); args.append(report_id)
    if verified is not None:
        sets.append("verified=?"); args.append(int(verified))
    if verify is not None:
        sets.append("verify_json=?"); args.append(json.dumps(verify) if verify else None)
    con.execute("UPDATE tickets SET " + ",".join(sets) + " WHERE id=?", args + [tid])
    con.execute("INSERT INTO ticket_history (ticket_id, ts, stage, note, contact_id, report_id) VALUES (?,?,?,?,?,?)", (tid, now, stage, note, contact_id, report_id))
    con.commit()


def history(con, tid=None, limit=100):
    sql = "SELECT h.id, h.ticket_id, h.ts, h.stage, h.note, h.contact_id, h.report_id, t.site, t.fault_key, t.verified FROM ticket_history h JOIN tickets t ON t.id=h.ticket_id"
    rows = con.execute(sql + (" WHERE h.ticket_id=?" if tid else "") + " ORDER BY h.id DESC LIMIT ?", ((tid, limit) if tid else (limit,))).fetchall()
    return [dict(id=r[0], ticket="T-%04d" % r[1], ticket_id=r[1], ts=r[2], stage=r[3], note=r[4], contact_id=r[5], report_id=r[6], site=r[7], fault_key=r[8],
                 verified=("measurement" if (r[4] or "").startswith("Verified by measurement") else "simulation" if (r[4] or "").startswith("Verified in SIMULATION") else None)) for r in rows]


def reports(con, limit=50):
    return [dict(id=r[0], ts=r[1], fault_id=r[2], fault_key=r[3], ticket_id=r[4], recipient=r[5], lang=r[6])
            for r in con.execute("SELECT id, ts, fault_id, fault_key, ticket_id, recipient, lang FROM reports ORDER BY ts DESC LIMIT ?", (limit,))]


def new_report_id(con):
    n = con.execute("SELECT COUNT(*) FROM reports").fetchone()[0] + 1
    return "R-%s-%04d" % (time.strftime("%y%m%d"), n)


# ---------------------------------------------------------------- drafts (never sent by SuryaNidaan)
def draft(lang, recipient, ctx, contact=None):
    """Subject and body customised to the fault. mailto:/tel: links are opened by the person in their own apps."""
    p = dict(node=ctx["node"], fault=ctx["fault"], when=ctx["when"], obs=ctx["obs"], steps=ctx["steps"], loc=ctx.get("loc", ""), report=ctx.get("report_id") or "-",
             source=ctx.get("source", ""))
    subject = i18n.t(lang, f"letter.{recipient}.subject", p)
    body = i18n.t(lang, f"letter.{recipient}.body", p)
    out = dict(recipient=recipient, subject=subject, body=body, to_name=contact["name"] if contact else "", to_email=contact["email"] if contact else "",
               to_phone=contact["phone"] if contact else "")
    out["mailto"] = ("mailto:%s?subject=%s&body=%s" % (urllib.parse.quote(contact["email"]), urllib.parse.quote(subject), urllib.parse.quote(body))) if contact and contact["email"] else ""
    out["tel"] = ("tel:" + "".join(ch for ch in contact["phone"] if ch.isdigit() or ch == "+")) if contact and contact["phone"] else ""
    return out


# ---------------------------------------------------------------- verification by measurement
def verify_decision(v, now, state, green_since, avg_ma):
    """Live node: 'Verified by measurement' only when the 2 s average current is back in the normal (green) state for green_s seconds.
    Returns (status, info): waiting | verified | not_verified."""
    held = (now - green_since) if (state == "green" and green_since) else 0.0
    info = dict(before_ma=v.get("before_ma"), after_ma=avg_ma, held_s=round(held, 1), green_s=v["green_s"], window_s=v["window_s"], elapsed_s=round(now - v["started"], 1))
    if state == "green" and held >= v["green_s"]:
        return "verified", info
    if now - v["started"] > v["window_s"]:
        return "not_verified", info
    return "waiting", info
