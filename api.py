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
DB_PATH = os.environ.get("SN_DB") or os.path.join(ROOT, "data", "suryanidaan.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
_lock = threading.Lock()


def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS sweeps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, node_id TEXT, source TEXT, seq INTEGER,
        payload TEXT, result TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, sweep_id INTEGER, text TEXT, sent INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS bench_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, node_id TEXT, kind TEXT, data TEXT)""")
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

from sn import bench


class Telemetry(BaseModel):
    node_id: str = "SN-001"
    source: str = Field("real", description='"real" from the sensor, "sim" if simulated')
    v: Optional[float] = Field(None, description="Bus voltage in V (null if the node does not report it)")
    ma: float = Field(..., description="Current in mA")
    raw: Optional[str] = Field(None, description="The raw serial line this reading came from")
    state: Optional[str] = Field(None, description="short / open / load / dark, from the 3 s rolling average")
    settled: bool = Field(False, description="True once the state has been stable for 2 s")
    v_avg: Optional[float] = None
    ma_avg: Optional[float] = None
    spread: Optional[float] = Field(None, description="Std-dev of the current over the 3 s window, mA")
    ldr: Optional[List[Optional[float]]] = Field(None, description="Raw ADC value of each LDR, in the order of data/sensors.json")
    temp_c: Optional[float] = Field(None, description="Panel temperature, only if a sensor really reports it")


_telemetry = {}       # node_id -> deque of the last 900 readings (15 min at 1 Hz)
_tele_meta = {}       # node_id -> latest raw line / state / rolling average


def _rate(rows, now):
    recent = [r["ts"] for r in rows if now - r["ts"] <= 10]
    return round((len(recent) - 1) / (recent[-1] - recent[0]), 2) if len(recent) > 2 and recent[-1] > recent[0] else 0.0


@app.post("/telemetry")
def post_telemetry(t: Telemetry):
    with _lock:
        _telemetry.setdefault(t.node_id, collections.deque(maxlen=900)).append(
            dict(ts=time.time(), source=t.source, v=t.v, ma=t.ma, state=t.state, ldr=t.ldr, temp_c=t.temp_c))
        _tele_meta[t.node_id] = dict(raw=t.raw, state=t.state, settled=t.settled, v_avg=t.v_avg,
                                     ma_avg=t.ma_avg, spread=t.spread, ts=time.time())
    return dict(ok=True)


@app.get("/telemetry")
def get_telemetry(node_id: str = "SN-001"):
    with _lock:
        rows = list(_telemetry.get(node_id, ()))
        meta = dict(_tele_meta.get(node_id, {}))
    now = time.time()
    return dict(node_id=node_id, now=now, readings=rows, meta=meta, rate=_rate(rows, now))


# ---------------------------------------------------------------- real bench (Isc / Voc against a baseline)
_bench = {}           # node_id -> {"short": pt, "open": pt, "load": pt, "dark": pt, "last_state", "last_ts", "fresh": set()}
STALE_S = 600         # a point older than this cannot be used to set a baseline


class BenchPoint(BaseModel):
    node_id: str = "SN-001"
    state: str = Field(..., description="short / open / load / dark")
    v: float
    ma: float
    spread: float = 0.0
    n: int = 0


class BaselineReq(BaseModel):
    node_id: str = "SN-001"


def _event(node, kind, data, ts=None):
    with _lock:
        con = db()
        cur = con.execute("INSERT INTO bench_events (ts, node_id, kind, data) VALUES (?,?,?,?)",
                          (ts or time.time(), node, kind, json.dumps(data)))
        con.commit()
        eid = cur.lastrowid
        con.close()
    return eid


def _baseline(node):
    rows = _rows("SELECT ts, data FROM bench_events WHERE node_id = ? AND kind = 'baseline' "
                 "ORDER BY id DESC LIMIT 1", (node,))
    if not rows:
        return None
    return dict(ts=rows[0][0], **json.loads(rows[0][1]))


def _bench_eval(node, kwp=C.DEFAULT_KWP, yld=C.DEFAULT_YIELD, tariff=C.DEFAULT_TARIFF):
    """Latest settled points, baseline, % changes, approximate loss and the rule-based finding."""
    st = _bench.get(node, {})
    sh, op, base = st.get("short"), st.get("open"), _baseline(node)
    out = dict(node_id=node, now=time.time(), source="real", approx=True, short=sh, open=op,
               load=st.get("load"), dark=st.get("dark"), baseline=base, isc_pct=None, voc_pct=None,
               loss_pct=None, money=None, finding=None, status="pending")
    out["assumptions"] = dict(kwp=kwp, yield_kwh_per_kwp_day=yld, tariff=tariff)
    if not base:
        out["finding"] = dict(key="finding.nobase")
        return out
    if not sh or not op:
        out["finding"] = dict(key="finding.needpts")
        return out
    out["isc_pct"] = bench.pct_change(sh["ma"], base["isc_ma"])
    out["voc_pct"] = bench.pct_change(op["v"], base["voc_v"])
    loss = bench.approx_loss(sh["ma"], op["v"], base["isc_ma"], base["voc_v"])
    out["loss_pct"] = round(loss * 100.0, 1)
    out["money"] = engine.money(loss, kwp, yld, tariff)
    out["finding"] = dict(key=bench.finding(out["isc_pct"], out["voc_pct"]))
    out["status"] = "ok"
    return out


@app.post("/bench/point")
def bench_point(p: BenchPoint):
    """A settled reading from serial_bridge.py (state stable for 2 s). Keeps the latest per state."""
    if p.state not in ("short", "open", "load", "dark"):
        raise HTTPException(422, "state must be short, open, load or dark")
    now = time.time()
    with _lock:
        st = _bench.setdefault(p.node_id, dict(fresh=set(), last_state=None, last_ts=0.0))
        new_instance = st["last_state"] != p.state or now - st["last_ts"] > 5
        st[p.state] = dict(v=p.v, ma=p.ma, spread=p.spread, n=p.n, ts=now)
        st["last_state"], st["last_ts"] = p.state, now
        if new_instance and p.state in ("short", "open"):
            st["fresh"].add(p.state)
        complete = {"short", "open"} <= st["fresh"]
    if complete and _baseline(p.node_id):
        ev = _bench_eval(p.node_id)
        if ev["status"] == "ok":              # one finding per completed measurement round
            with _lock:
                _bench[p.node_id]["fresh"].clear()
            m = ev["money"]
            _event(p.node_id, "finding", dict(key=ev["finding"]["key"], isc_ma=ev["short"]["ma"],
                   voc_v=ev["open"]["v"], isc_pct=ev["isc_pct"], voc_pct=ev["voc_pct"],
                   loss_pct=ev["loss_pct"], inr_day=m["inr_day"]))
    return dict(ok=True)


def _ldr_mean(node, now, secs=3.0):
    """Mean raw value of each LDR over the last few seconds (None where a sensor reported nothing)."""
    with _lock:
        rows = [r for r in _telemetry.get(node, ()) if now - r["ts"] <= secs and r.get("ldr")]
    if not rows:
        return None
    n = max(len(r["ldr"]) for r in rows)
    out = []
    for i in range(n):
        vals = [r["ldr"][i] for r in rows if i < len(r["ldr"]) and r["ldr"][i] is not None]
        out.append(sum(vals) / len(vals) if vals else None)
    return out


@app.post("/bench/baseline")
def bench_baseline(r: BaselineReq):
    """Store the latest settled Isc (jumper on) and Voc (jumper off) as the healthy baseline."""
    st = _bench.get(r.node_id, {})
    now = time.time()
    sh, op = st.get("short"), st.get("open")
    if not sh or not op or now - sh["ts"] > STALE_S or now - op["ts"] > STALE_S:
        raise HTTPException(409, "need_points")
    data = dict(isc_ma=sh["ma"], voc_v=op["v"], ldr_base=_ldr_mean(r.node_id, now))
    eid = _event(r.node_id, "baseline", data, now)
    with _lock:
        st["fresh"].clear()
    return dict(ok=True, id=eid, ts=now, **data)


@app.get("/bench/status")
def bench_status(node_id: str = "SN-001", kwp: float = C.DEFAULT_KWP,
                 yield_kwh_per_kwp_day: float = C.DEFAULT_YIELD, tariff: float = C.DEFAULT_TARIFF):
    """Latest values, baseline, % change and an APPROXIMATE loss, from real readings only."""
    return _bench_eval(node_id, kwp, yield_kwh_per_kwp_day, tariff)


@app.get("/bench/events")
def bench_events(node_id: Optional[str] = None, limit: int = 100):
    sql = "SELECT id, ts, node_id, kind, data FROM bench_events"
    args = ()
    if node_id:
        sql, args = sql + " WHERE node_id = ?", (node_id,)
    rows = _rows(sql + " ORDER BY id DESC LIMIT ?", args + (limit,))
    return [dict(id=i, ts=ts, node_id=n, kind=k, source="real", data=json.loads(d)) for i, ts, n, k, d in rows]


@app.get("/bench/curves")
def bench_curves(node_id: str = "SN-001"):
    """Physics-model curves (pvlib single diode) through the measured Isc and Voc. Models, not measurements."""
    ev = _bench_eval(node_id)
    base, sh, op = ev["baseline"], ev["short"], ev["open"]
    out = dict(baseline=None, now=None, loss_curve_pct=None, whatif=None,
               load=ev["load"] and dict(v=ev["load"]["v"], ma=ev["load"]["ma"]))
    if base:
        out["baseline"] = bench.anchored_curve(base["isc_ma"], base["voc_v"])
    if sh and op and op["v"] > 0.05 and sh["ma"] > 0:
        out["now"] = bench.anchored_curve(sh["ma"], op["v"])
    if out["baseline"] and out["now"]:
        loss = 1.0 - out["now"]["pmax_mw"] / out["baseline"]["pmax_mw"]
        out["loss_curve_pct"] = round(max(0.0, loss) * 100.0, 1)
        if out["loss_curve_pct"] >= 2.0:
            out["whatif"] = bench.whatif(int(round(min(out["loss_curve_pct"], 90))))
    return out


def _bench_row(kwp, yld, tariff):
    """Fleet row for the real bench node SN-001, from telemetry + bench state (never from a simulated sweep)."""
    ev = _bench_eval("SN-001", kwp, yld, tariff)
    rows = _telemetry.get("SN-001")
    last = rows[-1] if rows else None
    key = ev["finding"]["key"]
    label = {"finding.ok": "healthy", "finding.light": "less_light", "finding.voltage": "voltage_loss",
             "finding.unclear": "unclear"}.get(key, "no_baseline")
    m = ev["money"] or dict(inr_day=0.0, inr_year=0)
    return dict(site="SN-001", locality=BENCH_SITE[0], lat=BENCH_SITE[1], lon=BENCH_SITE[2], kwp=kwp, source="real",
                label=label, headline="", status="ok" if ev["status"] == "ok" else "pending", confidence=1.0,
                loss_pct=ev["loss_pct"] or 0.0, inr_day=m["inr_day"], inr_year=m["inr_year"], location=None,
                verdict=None, minutes_ago=int((time.time() - last["ts"]) / 60) if last else 0, ticket="",
                live=True, bench=True, finding=ev["finding"],
                power_mw=(last["v"] * last["ma"]) if last and last["v"] is not None else None,
                seconds_ago=int(time.time() - last["ts"]) if last else None)


# ---------------------------------------------------------------- web console
import functools

import numpy as np
from fastapi.responses import FileResponse

from sn import pv_sim as S
from sn.model import BENCH_PATH

WEB = os.path.join(ROOT, "web")


@app.get("/", include_in_schema=False)
def owner_page():
    """Owner view: Digital Twin & Health."""
    return FileResponse(os.path.join(WEB, "twin.html"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import Response
    return Response(status_code=204)


@app.get("/console", include_in_schema=False)
def console():
    """Installer view: the diagnostic console."""
    return FileResponse(os.path.join(WEB, "index.html"))


from fastapi.staticfiles import StaticFiles
os.makedirs(os.path.join(WEB, "vendor"), exist_ok=True)
app.mount("/vendor", StaticFiles(directory=os.path.join(WEB, "vendor")), name="vendor")


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


# ---------------------------------------------------------------- owner view: Digital Twin & Health
from sn import calib, health, profile as PROFILE, realdx, sensors as SENSORS, twin as TWIN

_session = collections.deque(maxlen=3600)      # (ts, score, light_pct) sampled by the watcher, for trend and session chart
_alert_log = {}                                # level -> last alert time (max one per 60 s per level)
_level = dict(shown=None)                      # state currently shown (hysteresis is applied against it)
NODE = "SN-001"


def _twin_ctx(tariff=C.DEFAULT_TARIFF, yld=C.DEFAULT_YIELD, kwp=C.DEFAULT_KWP):
    now = time.time()
    with _lock:
        rows = list(_telemetry.get(NODE, ()))
        meta = dict(_tele_meta.get(NODE, {}))
    return dict(now=now, readings=rows, meta=meta, bst=_bench_eval(NODE, kwp, yld, tariff), cal=calib.load(),
                profile=PROFILE.get(), sensors_cfg=SENSORS.load(), rate=_rate(rows, now), kwp=kwp, yld=yld, tariff=tariff,
                hist=[(t, sc) for t, sc, _ in _session], prev_state=_level["shown"])


@app.get("/twin/state")
def twin_state(tariff: float = C.DEFAULT_TARIFF, yield_kwh_per_kwp_day: float = C.DEFAULT_YIELD):
    """REAL data only: telemetry + bench points + calibration + LDRs + profile -> everything the owner page shows."""
    return TWIN.live(_twin_ctx(tariff, yield_kwh_per_kwp_day))


@app.post("/twin/sim")
def twin_sim(r: SimReq):
    """SIMULATED: the existing /simulate (physics twin + injected faults), shaped like the live view."""
    out = simulate(r)
    v = TWIN.sim(r.model_dump() if hasattr(r, "model_dump") else r.dict(), out["payload"], dict(out["result"], ts=out["ts"]),
                 PROFILE.get(), None)
    v["now"] = out["ts"]
    v["sweep"] = dict(points=out["payload"]["points"], expected=out["result"]["expected_points"])
    return v


@app.get("/twin/history")
def twin_history(seconds: int = 900):
    now = time.time()
    return [dict(ts=t, score=sc, light=li) for t, sc, li in _session if now - t <= seconds]


@app.get("/twin/alerts")
def twin_alerts(limit: int = 20):
    rows = _rows("SELECT id, ts, text, sent FROM alerts WHERE sweep_id = 0 ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(id=i, ts=ts, text=t, sent=bool(sn)) for i, ts, t, sn in rows]


def _health_alert(v, prev, level):
    """Telegram + alerts list when REAL health moves to amber/red or recovers. Max one per 60 s per level."""
    now = time.time()
    if now - _alert_log.get(level, 0) < 60:
        return
    _alert_log[level] = now
    lang = _settings()["lang"]
    m = v.get("money") or {}
    top = (v.get("diagnosis") or {}).get("top")
    key = "tg.recovered" if level == "healthy" else "tg." + level
    text = i18n.t(lang, key, dict(node=NODE, drop=m.get("loss_pct", 0), inr=m.get("inr_day", 0), kwp=m.get("kwp", ""),
                                  cause=dict(key="f." + top, p={}) if top else "", score=(v.get("health") or {}).get("score", "")))
    sent = alerts.send(text)
    with _lock:
        con = db()
        con.execute("INSERT INTO alerts (ts, sweep_id, text, sent) VALUES (?,?,?,?)", (now, 0, text, int(sent)))
        con.commit()
        con.close()
    _event(NODE, "health", dict(level=level, previous=prev, score=(v.get("health") or {}).get("score"), text=text))


def _watch():
    cand, streak, level = None, 0, None
    while True:
        time.sleep(2)
        try:
            v = TWIN.live(_twin_ctx())
            h = v.get("health")
            _session.append((v["now"], h["score"] if h else None, (v["sensors"] or {}).get("light_pct") if v["online"] else None))
            st = v["state"] if v["state"] in ("healthy", "attention", "critical") else None
            if st is None:
                continue                      # offline / no data / no baseline: keep the last level, never alert
            _level["shown"] = st
            cand, streak = (st, streak + 1) if st == cand else (st, 1)
            if streak >= 3 and st != level:   # must hold for ~6 s
                prev, level = level, st
                if prev is not None:
                    _health_alert(v, prev, st)
        except Exception:
            pass


threading.Thread(target=_watch, daemon=True, name="health-watch").start()


class CalVerify(BaseModel):
    meter_ma: float = Field(..., description="Multimeter reading in series, mA, taken at the same moment")


@app.get("/calibration")
def get_calibration():
    now = time.time()
    with _lock:
        rows = [r["ma"] for r in _telemetry.get(NODE, ()) if now - r["ts"] <= 10]
    return dict(calibration=calib.load(), live=dict(n=len(rows), ina_avg_ma=(sum(rows) / len(rows)) if rows else None,
                                                    spread=(float(np.std(rows)) if rows else None)))


@app.post("/calibration/verify")
def verify_calibration(c: CalVerify):
    """gain = multimeter / INA219 average over the last 10 s. Nothing ever auto-adjusts."""
    now = time.time()
    with _lock:
        rows = [r["ma"] for r in _telemetry.get(NODE, ()) if now - r["ts"] <= 10]
    if len(rows) < 5:
        raise HTTPException(422, "cal.err.nodata")
    try:
        return calib.verify(c.meter_ma, sum(rows) / len(rows))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.post("/calibration/reset")
def reset_calibration():
    return calib.reset()


@app.get("/sensors")
def get_sensors():
    with _lock:
        rows = list(_telemetry.get(NODE, ()))
    last = rows[-1] if rows else None
    base = _baseline(NODE)
    live = SENSORS.normalise(last.get("ldr") if last and time.time() - last["ts"] <= 10 else None, base.get("ldr_base") if base else None)
    return dict(sensors=live, temperature_fitted=bool(last and last.get("temp_c") is not None))


@app.get("/profile")
def get_profile():
    return dict(active=PROFILE.active_id(), profiles=PROFILE.all_profiles(), fields=PROFILE.FIELDS)


class ProfileReq(BaseModel):
    id: str = "custom"
    fields: dict


@app.post("/profile")
def save_profile(r: ProfileReq):
    p = PROFILE.save(r.id, r.fields)
    if PROFILE.has_datasheet(p):
        try:
            PROFILE.fit(p)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    return p


class ProfileActive(BaseModel):
    id: str


@app.post("/profile/active")
def set_profile_active(r: ProfileActive):
    try:
        PROFILE.set_active(r.id)
    except KeyError:
        raise HTTPException(404, "unknown profile")
    return PROFILE.get()


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
def fleet(yield_kwh_per_kwp_day: float = C.DEFAULT_YIELD, tariff: float = C.DEFAULT_TARIFF,
          kwp: float = C.DEFAULT_KWP):
    kwp_arg = kwp
    rows = []
    for row, _, _ in _fleet(1.0, yield_kwh_per_kwp_day, tariff).values():
        row = dict(row)
        row["ticket"] = _last_ticket(row["site"]) or row["ticket"]
        rows.append(row)
    if _telemetry.get("SN-001"):
        rows.append(_bench_row(1.0 * kwp_arg, yield_kwh_per_kwp_day, tariff))
    for node in (("SN-SIM-01",) if _telemetry.get("SN-001") else ("SN-001", "SN-SIM-01")):
        lat = latest(node)
        if lat:
            res = lat["result"]
            if "location" not in res:      # sweep stored by an older version of the engine
                continue
            rows.append(_row(node, BENCH_SITE[0], BENCH_SITE[1], BENCH_SITE[2] + (0.012 if node == "SN-SIM-01" else 0),
                             res["assumptions"]["kwp"], lat["source"], res, int((time.time() - lat["ts"]) / 60),
                             _last_ticket(node), live=True, sweep_id=lat["id"]))
    rows.sort(key=lambda r: (not r.get("bench"), -r["inr_day"]))
    dist = {}
    for r in rows:
        dist[r["label"]] = dist.get(r["label"], 0) + 1
    return dict(rows=rows, total=len(rows), healthy=dist.get("healthy", 0),
                attention=len(rows) - dist.get("healthy", 0) - dist.get("no_baseline", 0),
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
