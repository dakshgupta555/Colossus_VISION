"""SuryaNidaan backend.

Run:  uvicorn api:app --host 0.0.0.0 --port 8000
The ESP32 (and virtual_node.py) POST sweeps to  http://<laptop-ip>:8000/sweeps
Interactive docs: http://localhost:8000/docs
"""
import json
import os
import sqlite3
import threading
import time
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from sn import alerts, config as C, engine, i18n

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "data", "suryanidaan.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
_lock = threading.Lock()


def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS sweeps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, node_id TEXT, source TEXT, seq INTEGER,
        payload TEXT, result TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, sweep_id INTEGER, text TEXT, sent INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, site TEXT, label TEXT, sweep_id INTEGER, status TEXT)""")
    return con


class Sweep(BaseModel):
    node_id: str = Field("SN-001", description="Unique node ID")
    seq: int = Field(0, description="Sweep sequence number from the node")
    source: str = Field("real", description='"real" from the ESP32, "sim" from the simulator')
    temp_c: float = Field(..., description="Panel temperature, deg C")
    ref_isc_ma: float = Field(..., description="Reference cell short-circuit current, mA")
    points: List[List[float]] = Field(..., description="[[volts, milliamps], ...] incl. the Voc point")
    mid_v: Optional[List[float]] = Field(None, description="Optional: volts across panel A (ground side) at "
                                         "each sweep point, from a divider on the junction between the panels. "
                                         "Lets the engine say WHICH panel is shaded or bypassed.")
    fw: Optional[str] = None
    kwp: float = C.DEFAULT_KWP
    yield_kwh_per_kwp_day: float = C.DEFAULT_YIELD
    tariff: float = C.DEFAULT_TARIFF


app = FastAPI(title="SuryaNidaan diagnostic API", version="0.1")


@app.on_event("startup")
def _warm():
    engine.bundle()  # loads (or trains) the model once
    _fleet(1.0, C.DEFAULT_YIELD, C.DEFAULT_TARIFF)  # pre-compute the simulated fleet so the tab opens instantly


def _retry_pending(con):
    for aid, text in con.execute("SELECT id, text FROM alerts WHERE sent = 0 ORDER BY id").fetchall():
        if alerts.send(text):
            con.execute("UPDATE alerts SET sent = 1 WHERE id = ?", (aid,))
        else:
            break
    con.commit()


def _store(payload, res):
    with _lock:
        con = db()
        cur = con.execute("INSERT INTO sweeps (ts, node_id, source, seq, payload, result) VALUES (?,?,?,?,?,?)",
                          (time.time(), payload["node_id"], payload["source"], payload.get("seq", 0),
                           json.dumps(payload), json.dumps(res)))
        sweep_id = cur.lastrowid
        alert_state = "none"
        if res["label"] != "healthy" or res["status"] != "ok":
            text = alerts.format_alert(payload["node_id"], payload["source"], res, _settings()["lang"])
            con.execute("INSERT INTO alerts (ts, sweep_id, text, sent) VALUES (?,?,?,0)",
                        (time.time(), sweep_id, text))
            con.commit()
            _retry_pending(con)
            sent = con.execute("SELECT sent FROM alerts WHERE sweep_id = ?", (sweep_id,)).fetchone()[0]
            alert_state = "sent" if sent else ("queued" if alerts.load_cfg() else "not configured")
        con.commit()
        con.close()
    return sweep_id, alert_state


SETTINGS_PATH = os.path.join(ROOT, "data", "settings.json")


def _settings():
    try:
        return {"lang": "en", **json.load(open(SETTINGS_PATH))}
    except (OSError, ValueError):
        return {"lang": "en"}


class Settings(BaseModel):
    lang: str = "en"


@app.get("/settings")
def get_settings():
    return _settings()


@app.post("/settings")
def set_settings(s: Settings):
    """Console language; Telegram alerts are sent in the same language."""
    if s.lang not in i18n.LANGS:
        raise HTTPException(422, "unknown language")
    json.dump({"lang": s.lang}, open(SETTINGS_PATH, "w"))
    return _settings()


@app.get("/i18n")
def get_i18n():
    return dict(langs=i18n.LANGS, strings=i18n.STRINGS)


def _led(res):
    if res["label"] == "healthy" and res["status"] == "ok":
        return "green"
    return "amber" if res["status"] != "ok" else "red"


@app.post("/sweeps")
def post_sweep(s: Sweep):
    if len(s.points) < 10:
        raise HTTPException(422, "need at least 10 points")
    payload = s.model_dump() if hasattr(s, "model_dump") else s.dict()
    res = engine.diagnose(payload, kwp=s.kwp, yld=s.yield_kwh_per_kwp_day, tariff=s.tariff)
    sweep_id, alert_state = _store(payload, res)
    return dict(id=sweep_id, led=_led(res), label=res["label"], headline=res["headline"],
                confidence=res["confidence"], loss_pct=res["loss_pct"], action=res["action"],
                alert=alert_state)


def _rows(sql, args=()):
    con = db()
    rows = con.execute(sql, args).fetchall()
    con.close()
    return rows


@app.get("/latest")
def latest(node_id: Optional[str] = None):
    if node_id:
        rows = _rows("SELECT id, ts, node_id, source, payload, result FROM sweeps WHERE node_id = ? "
                     "ORDER BY id DESC LIMIT 1", (node_id,))
    else:
        rows = _rows("SELECT id, ts, node_id, source, payload, result FROM sweeps ORDER BY id DESC LIMIT 1")
    if not rows:
        return {}
    i, ts, node, src, p, r = rows[0]
    return dict(id=i, ts=ts, node_id=node, source=src, payload=json.loads(p), result=json.loads(r))


@app.get("/history")
def history(limit: int = 50):
    out = []
    for i, ts, node, src, r in _rows("SELECT id, ts, node_id, source, result FROM sweeps ORDER BY id DESC LIMIT ?",
                                     (limit,)):
        res = json.loads(r)
        out.append(dict(id=i, ts=ts, node_id=node, source=src, headline=res["headline"], label=res["label"],
                        status=res["status"], location=res.get("location"),
                        confidence=res["confidence"], loss_pct=res["loss_pct"], inr_day=res["money"]["inr_day"],
                        rule_label=res["rule_label"]))
    return out


@app.get("/sweep/{sweep_id}")
def one(sweep_id: int):
    rows = _rows("SELECT id, ts, node_id, source, payload, result FROM sweeps WHERE id = ?", (sweep_id,))
    if not rows:
        raise HTTPException(404, "not found")
    i, ts, node, src, p, r = rows[0]
    return dict(id=i, ts=ts, node_id=node, source=src, payload=json.loads(p), result=json.loads(r))


@app.get("/health")
def health():
    return dict(ok=True, telegram=bool(alerts.load_cfg()))


# ---------------------------------------------------------------- live telemetry (in memory)
import collections


class Telemetry(BaseModel):
    node_id: str = "SN-001"
    source: str = Field("real", description='"real" from the sensor, "sim" if simulated')
    v: Optional[float] = Field(None, description="Bus voltage in V (null if the node does not report it)")
    ma: float = Field(..., description="Current in mA")


_telemetry = {}   # node_id -> deque of the last 300 readings


@app.post("/telemetry")
def post_telemetry(t: Telemetry):
    with _lock:
        _telemetry.setdefault(t.node_id, collections.deque(maxlen=300)).append(
            dict(ts=time.time(), source=t.source, v=t.v, ma=t.ma))
    return dict(ok=True)


@app.get("/telemetry")
def get_telemetry(node_id: str = "SN-001"):
    with _lock:
        rows = list(_telemetry.get(node_id, ()))
    return dict(node_id=node_id, now=time.time(), readings=rows)


# ---------------------------------------------------------------- web console
import functools

import numpy as np
from fastapi.responses import FileResponse

from sn import pv_sim as S
from sn.model import BENCH_PATH

WEB = os.path.join(ROOT, "web")


@app.get("/", include_in_schema=False)
def console():
    return FileResponse(os.path.join(WEB, "index.html"))


@app.get("/static/plotly.min.js", include_in_schema=False)
def plotly_js():
    import plotly
    return FileResponse(os.path.join(os.path.dirname(plotly.__file__), "package_data", "plotly.min.js"),
                        media_type="application/javascript")


class SimReq(BaseModel):
    light: float = 0.9
    temp: float = 38.0
    shade: float = 0.0
    soil: float = 0.0
    rx: float = 0.0
    short: bool = False
    noise: bool = True
    panel: int = 1          # which panel the shade / shorted diode is on: 0 = A, 1 = B
    mid: bool = True        # include the mid-point sensor reading
    seed: int = 1
    store: bool = False
    kwp: float = C.DEFAULT_KWP
    yield_kwh_per_kwp_day: float = C.DEFAULT_YIELD
    tariff: float = C.DEFAULT_TARIFF


@app.post("/simulate")
def simulate(r: SimReq):
    """SIMULATED test-bench sweep: physics twin + injected faults (never presented as a measurement)."""
    scn = dict(g=r.light, temp_c=r.temp, iph_scale=[1.0, 1.0], rs_scale=[1.0, 1.0], soiling=r.soil,
               shade=r.shade, shade_panel=r.panel, extra_r=r.rx, shorted=r.panel if r.short else None)
    p = S.simulate(scn, np.random.default_rng(r.seed), noise=r.noise)
    if not r.mid:
        p.pop("mid_v", None)
    p.update(node_id="SN-SIM-01", seq=r.seed, source="sim", fw="test-bench")
    res = engine.diagnose(p, kwp=r.kwp, yld=r.yield_kwh_per_kwp_day, tariff=r.tariff)
    out = dict(payload=p, result=res, ts=time.time())
    if r.store:
        sweep_id, alert_state = _store(p, res)
        out.update(id=sweep_id, alert=alert_state, led=_led(res))
    return out


@app.get("/benchmark")
def benchmark():
    b = json.load(open(BENCH_PATH)) if os.path.exists(BENCH_PATH) else {}
    bun = engine.bundle()
    b["importances"] = bun.get("importances", {})
    b["n_train"] = bun.get("n_train")
    b["trained_on"] = bun.get("trained_on")
    return b


# Simulated fleet: approximate coordinates of Bengaluru localities (for the schematic site map only).
LOCALITIES = [("Jayanagar", 12.925, 77.594), ("Koramangala", 12.935, 77.624), ("Whitefield", 12.970, 77.750),
              ("Indiranagar", 12.972, 77.641), ("HSR Layout", 12.912, 77.645), ("Malleshwaram", 13.003, 77.570),
              ("Rajajinagar", 12.991, 77.553), ("Banashankari", 12.925, 77.547), ("Yelahanka", 13.101, 77.596),
              ("Hebbal", 13.036, 77.597), ("BTM Layout", 12.917, 77.610), ("Basavanagudi", 12.941, 77.575),
              ("JP Nagar", 12.907, 77.586), ("Electronic City", 12.845, 77.660), ("Marathahalli", 12.957, 77.701),
              ("Vijayanagar", 12.972, 77.535), ("RR Nagar", 12.927, 77.519), ("Bellandur", 12.926, 77.676),
              ("Kengeri", 12.914, 77.483), ("Sahakara Nagar", 13.062, 77.587), ("Frazer Town", 12.998, 77.613),
              ("Banaswadi", 13.014, 77.652), ("Ulsoor", 12.982, 77.620), ("Domlur", 12.961, 77.638)]
BENCH_SITE = ("Test bench, Dr. AIT", 12.964, 77.506)


def _row(site, locality, lat, lon, kwp, source, res, minutes_ago, ticket, **extra):
    return dict(site=site, locality=locality, lat=lat, lon=lon, kwp=kwp, source=source, label=res["label"],
                headline=res["headline"], status=res["status"], confidence=res["confidence"],
                loss_pct=res["loss_pct"], inr_day=res["money"]["inr_day"], inr_year=res["money"]["inr_year"],
                location=res["location"], verdict=res["verdict"], minutes_ago=minutes_ago, ticket=ticket, **extra)


@functools.lru_cache(maxsize=8)
def _fleet(kwp_scale, yld, tariff):
    """24 SIMULATED sites. Returns {site: (row, payload, result)}."""
    rng = np.random.default_rng(47)
    weights = dict(healthy=0.6, soiling=0.18, partial_shade=0.1, series_resistance=0.08, bypass_short=0.04)
    out = {}
    for i, (loc, lat, lon) in enumerate(LOCALITIES):
        fault = rng.choice(list(weights), p=list(weights.values()))
        s = S.make_scenario(fault, rng, hard=bool(rng.random() < 0.6))  # mostly mild faults, like a real fleet
        s["g"] = rng.uniform(0.6, 1.0)
        kwp = float(np.round(rng.uniform(2, 6) * 2) / 2)
        payload = S.simulate(s, rng)
        site = f"SIM-{101 + i}"
        payload.update(node_id=site, source="sim", seq=0, fw="fleet-sim")
        res = engine.diagnose(payload, kwp=kwp, yld=yld, tariff=tariff)
        faulty = res["label"] != "healthy" or res["status"] != "ok"
        row = _row(site, loc, lat, lon, kwp, "sim", res, int(rng.integers(2, 180)),
                   ("Open" if faulty and rng.random() < 0.6 else ("Assigned" if faulty else "")))
        out[site] = (row, payload, res)
    return out


def _last_ticket(site):
    rows = _rows("SELECT id FROM tickets WHERE site = ? ORDER BY id DESC LIMIT 1", (site,))
    return f"T-{rows[0][0]:04d}" if rows else ""


@app.get("/fleet")
def fleet(yield_kwh_per_kwp_day: float = C.DEFAULT_YIELD, tariff: float = C.DEFAULT_TARIFF):
    rows = []
    for row, _, _ in _fleet(1.0, yield_kwh_per_kwp_day, tariff).values():
        row = dict(row)
        row["ticket"] = _last_ticket(row["site"]) or row["ticket"]
        rows.append(row)
    for node in ("SN-001", "SN-SIM-01"):
        lat = latest(node)
        if lat:
            res = lat["result"]
            if "location" not in res:      # sweep stored by an older version of the engine
                continue
            rows.append(_row(node, BENCH_SITE[0], BENCH_SITE[1], BENCH_SITE[2] + (0.012 if node == "SN-SIM-01" else 0),
                             res["assumptions"]["kwp"], lat["source"], res, int((time.time() - lat["ts"]) / 60),
                             _last_ticket(node), live=True, sweep_id=lat["id"]))
    rows.sort(key=lambda r: -r["inr_day"])
    dist = {}
    for r in rows:
        dist[r["label"]] = dist.get(r["label"], 0) + 1
    return dict(rows=rows, total=len(rows), healthy=dist.get("healthy", 0),
                attention=len(rows) - dist.get("healthy", 0),
                inr_day_at_risk=round(sum(r["inr_day"] for r in rows), 1), distribution=dist)


@app.get("/fleet/site/{site}")
def fleet_site(site: str, yield_kwh_per_kwp_day: float = C.DEFAULT_YIELD, tariff: float = C.DEFAULT_TARIFF):
    """Full sweep and diagnosis of one SIMULATED fleet site (for the drill-down)."""
    hit = _fleet(1.0, yield_kwh_per_kwp_day, tariff).get(site)
    if not hit:
        raise HTTPException(404, "unknown site")
    row, payload, res = hit
    return dict(site=site, locality=row["locality"], source="sim", payload=payload, result=res,
                ts=time.time() - row["minutes_ago"] * 60)


class TicketReq(BaseModel):
    site: str
    label: str = ""
    sweep_id: Optional[int] = None


@app.post("/tickets")
def create_ticket(t: TicketReq):
    with _lock:
        con = db()
        cur = con.execute("INSERT INTO tickets (ts, site, label, sweep_id, status) VALUES (?,?,?,?,?)",
                          (time.time(), t.site, t.label, t.sweep_id, "open"))
        con.commit()
        tid = cur.lastrowid
        con.close()
    return dict(id=f"T-{tid:04d}", site=t.site, status="open")
