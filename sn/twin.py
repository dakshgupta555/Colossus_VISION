"""View model for the owner-facing "Digital Twin & Health" page.

live(ctx)  -> REAL data: telemetry + bench points + calibration + LDRs + panel profile
sim(...)   -> SIMULATED data: the existing /simulate result (engine.diagnose) shaped the same way

Everything the page shows as text is an i18n key + params (sn/i18n.py). Nothing here invents a measurement:
a missing sensor is None ("Not fitted"), no data is no score.
"""
import math

import numpy as np

from . import bench, calib, engine, health, profile as PR, realdx, sensors

OFFLINE_S = 10.0
CLASSES_REAL = realdx.CLASSES
CLASSES_SIM = ["healthy", "soiling", "partial_shade", "series_resistance", "bypass_short"]
IMPACT = dict(healthy=0.0, soiling=1.0, partial_shade=1.0, series_resistance=0.9, light_change=0.6, bypass_short=1.2)


def _f(x, d=None):
    return d if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def _badge_current(cal):
    return "CALIBRATED" if cal["status"] == "VERIFIED" else "PROVISIONAL"


def _cards_and_actions(probs, order, loss_frac, money, have_ref_note=True):
    """'What might be happening?' cards and ranked actions (impact x probability). Never asks the owner to touch equipment."""
    cards = []
    for c in order:
        if probs[c] < 0.10 and cards:
            continue
        cards.append(dict(cls=c, p=round(probs[c], 3), name="f." + c, one="card.%s.one" % c, symptom="card.%s.sym" % c,
                          cause="card.%s.cause" % c, action="act.%s" % c))
    ranked = sorted([c for c in order if c != "healthy"], key=lambda c: -probs[c] * IMPACT.get(c, 1.0))
    acts = [dict(cls=c, key="act." + c, score=round(probs[c] * IMPACT.get(c, 1.0), 3),
                 inr_day=round(money["inr_day"] * probs[c], 1) if money else None) for c in ranked if probs[c] >= 0.10][:3]
    if not acts:
        acts = [dict(cls="healthy", key="act.healthy", score=0, inr_day=None)]
    return cards[:4], acts


def _twin_block(state, light, prof, rows=None, analysis=None, sim=None, caption=None):
    return dict(state=state, light=light, grid=dict(x=prof.get("cells_x") or 6, y=prof.get("cells_y") or 6),
                ldr=[r for r in (rows or []) if r["pos"] and r["rel_pct"] is not None] if rows else [],
                regions=(analysis or {}).get("shaded", []) if analysis else [], sim=sim, caption=caption)


def _series_from_readings(readings, isc_base_cal, cal, now, window_s=600):
    """(ts, Isc ratio) from SHORT-state readings: what the time-pattern detector reads."""
    if not isc_base_cal:
        return []
    return [(r["ts"], calib.apply(r["ma"], cal) / isc_base_cal) for r in readings
            if r.get("state") == "short" and now - r["ts"] <= window_s]


def live(ctx):
    now, rs, meta, bst, cal = ctx["now"], ctx["readings"], ctx.get("meta") or {}, ctx["bst"], ctx["cal"]
    prof, cfg = ctx["profile"], ctx["sensors_cfg"]
    last = rs[-1] if rs else None
    online = bool(last and now - last["ts"] <= OFFLINE_S)
    base, sh, op = bst["baseline"], bst["short"], bst["open"]
    out = dict(mode="live", source="REAL", node_id=bst["node_id"], now=now, online=online,
               last_ts=last["ts"] if last else None, age_s=(now - last["ts"]) if last else None, rate=ctx.get("rate", 0.0),
               profile=dict(id=prof["id"], name=prof["name"], kwp=prof.get("kwp")), calibration=cal,
               badges=dict(voltage="REAL", current=_badge_current(cal), light="REAL", temperature="REAL", estimate="PHYSICS MODEL",
                           money="PROVISIONAL", health="PROVISIONAL"))
    # ---- live readings (never shown as live when the node is offline)
    ina_state = meta.get("state") if online else None
    reading = None
    if last:
        ma_cal = calib.apply(last["ma"], cal)
        reading = dict(v=last["v"], ma_raw=last["ma"], ma_cal=ma_cal, ts=last["ts"], live=online,
                       p_mw=(last["v"] * ma_cal) if last["v"] is not None else None, state=ina_state,
                       settled=bool(meta.get("settled")) if online else False, spread=meta.get("spread"), raw_line=meta.get("raw"))
    out["reading"] = reading
    isc_base_cal = calib.apply(base["isc_ma"], cal) if base else None
    # ---- sensors: LDRs (relative to their own baseline) and temperature
    rows = sensors.normalise(last.get("ldr") if (last and online) else None, base.get("ldr_base") if base else None, cfg)
    ana = sensors.analyse(rows)
    temp = last.get("temp_c") if (last and online) else None
    cur_pct, cur_src = None, None
    if isc_base_cal:
        if online and ina_state == "short" and reading:
            cur_pct, cur_src = reading["ma_cal"] / isc_base_cal * 100, "live"
        elif sh:
            cur_pct, cur_src = calib.apply(sh["ma"], cal) / isc_base_cal * 100, "point"
    ref = next((r for r in rows if r["role"] == "reference" and r["rel_pct"] is not None), None)
    light_pct, light_src = (ref["rel_pct"], "ldr") if ref else (cur_pct, "current" if cur_pct is not None else None)
    out["sensors"] = dict(ldr=rows, temperature=temp, temperature_fitted=temp is not None, ldr_fitted=any(r["raw"] is not None for r in rows),
                          light_pct=light_pct, light_src=light_src, current_pct=cur_pct, current_pct_src=cur_src, analysis=ana)
    out["baseline"] = base
    out["points"] = dict(
        isc=dict(raw=sh["ma"], cal=calib.apply(sh["ma"], cal), ts=sh["ts"]) if sh else None,
        voc=dict(v=op["v"], ts=op["ts"]) if op else None)
    quality = health.data_quality(ctx.get("rate", 0.0), meta.get("spread"), cal["status"], online)
    out["quality"] = quality
    # ---- state machine for the hero
    hist = ctx.get("hist") or []
    out["diagnosis"] = out["health"] = out["money"] = out["expected"] = out["cards"] = None
    out["actions"] = []
    if not last:
        out["state"], out["status_key"] = "nodata", "st.nodata"
    elif not online:
        out["state"], out["status_key"] = "offline", "st.offline"
    elif not base:
        out["state"], out["status_key"] = "setup", "st.nobase"
    elif not sh or not op:
        out["state"], out["status_key"] = "setup", "st.needpts"
    else:
        isc_ratio = calib.apply(sh["ma"], cal) / isc_base_cal
        voc_ratio = op["v"] / base["voc_v"]
        pat, pat_d = realdx.pattern(_series_from_readings(rs, isc_base_cal, cal, now))
        dx = realdx.diagnose(isc_ratio, voc_ratio, isc_base_cal, ana["ref_ratio"], pat, pat_d, bst["finding"]["key"],
                             quality["score"], ana)
        h = health.real(isc_ratio, voc_ratio)
        h["state"] = health.state_hyst(h["score"], ctx.get("prev_state"))
        prev, arrow = health.trend(hist, h["score"], now_ts=now)
        loss = bench.approx_loss(isc_ratio, voc_ratio, 1.0, 1.0)
        m = engine.money(loss, prof.get("kwp") or ctx["kwp"], ctx["yld"], ctx["tariff"])
        out.update(state=h["state"], status_key="st." + h["state"])
        out["diagnosis"] = dict(dx, classes=CLASSES_REAL, source="REAL")
        out["health"] = dict(h, previous=prev, trend=arrow)
        out["money"] = dict(m, loss_pct=round(loss * 100, 1), kwp=prof.get("kwp") or ctx["kwp"], tariff=ctx["tariff"], provisional=True)
        out["cards"], out["actions"] = _cards_and_actions(dx["probs"], dx["order"], loss, m)
        out["expected"] = _expected_real(prof, base, sh, op, cal, ana, isc_base_cal)
        out["loss_pct"] = round(loss * 100, 1)
    # ---- the twin the page draws
    cap = "twin.cap.whole"
    st = out["state"]
    if ana["shaded"]:
        cap = "twin.cap.region"
    out["twin"] = _twin_block(st, light_pct if online else None, prof, rows, ana if ana["shaded"] else None, caption=cap)
    out["twin"]["regions"] = ana["shaded"] if online else []
    return out


def _expected_real(prof, base, sh, op, cal, ana, isc_base_cal):
    """Expected vs measured. Expected = the baseline scaled by the light change the reference LDR reports (if any)."""
    ratio = ana["ref_ratio"] if ana["ref_ratio"] else 1.0
    r = bench._knee_ratio()
    isc_exp = isc_base_cal * ratio
    voc_exp = base["voc_v"] * (1 + r * math.log(max(ratio, 0.05)))
    isc_meas = calib.apply(sh["ma"], cal)
    p_exp = bench.anchored_curve(isc_exp, voc_exp)["pmax_mw"]
    p_meas = bench.anchored_curve(max(isc_meas, 0.01), op["v"])["pmax_mw"] if op["v"] > 0.05 else None
    eq = PR.equivalent_light_w_m2(isc_meas, prof)
    out = dict(rows=[dict(key="isc", unit="mA", expected=isc_exp, measured=isc_meas, badge="REAL"),
                     dict(key="voc", unit="V", expected=voc_exp, measured=op["v"], badge="REAL"),
                     dict(key="pmax", unit="mW", expected=p_exp, measured=p_meas, badge="PHYSICS MODEL")],
               basis="ldr" if ana["ref_ratio"] else "baseline", equivalent_light=eq, equivalent_pct=(eq / 10.0) if eq is not None else None)
    if PR.has_datasheet(prof):
        try:
            e = PR.expected(prof, (eq or 0) / 1000.0, 25.0)
            out["datasheet"] = dict(isc_a=e["isc_a"], voc_v=e["voc_v"], pmax_w=e["pmax_w"], measured_voc_v=op["v"],
                                    mismatch=not (0.7 <= op["v"] / e["voc_v"] <= 1.3))
        except ValueError:
            out["datasheet"] = None
    return out


# ------------------------------------------------------------------------------------------------ simulated
def _shaded_cells(frac, nx, ny):
    """Cells covered when `frac` of the panel is shaded, filled from the top-left corner row by row."""
    n = int(round(frac * nx * ny))
    return [[i % nx, i // nx] for i in range(n)]


def sim(req, payload, res, prof, ctx_money):
    """Shape an engine.diagnose() result of the physics twin like the live view. Highlights exactly what was injected."""
    h = health.sweep(res)
    probs = {c: res["probs"].get(c, 0.0) for c in CLASSES_SIM}
    order = sorted(CLASSES_SIM, key=probs.get, reverse=True)
    f = res["features"]
    cert = realdx._certainty(probs, res["rules_agree"], not res["out_of_distribution"], None)
    nx, ny = prof.get("cells_x") or 6, prof.get("cells_y") or 6
    simmod = dict(shade_cells=_shaded_cells(req["shade"], nx, ny) if req["shade"] > 0 else [], dust=req["soil"],
                  joint=req["rx"] > 0, diode=bool(req["short"]))
    cards, acts = _cards_and_actions(probs, order, res["loss_pct"] / 100.0, res["money"])
    ev_top = res.get("ev") or []
    dx = dict(probs=probs, order=order, top=order[0], others=[c for c in order[1:] if probs[c] >= 0.10],
              evidence={c: [dict(key=i["key"], p=i["p"]) for i in ev_top] if c == order[0] else [] for c in CLASSES_SIM},
              tell_apart=["ta.sim"], certainty=cert, features={k: f[k] for k in ("isc_ratio", "voc_ratio", "pmax_ratio", "ff_ratio",
                                                                                   "rs_ratio", "step_score", "n_peaks")},
              classes=CLASSES_SIM, source="SIMULATED", rule_label=res["rule_label"], uses_ldr=False, bypass="applicable",
              status=res["status"], pattern="unknown", pattern_detail={})
    m = dict(res["money"], loss_pct=res["loss_pct"], kwp=res["assumptions"]["kwp"], tariff=res["assumptions"]["tariff_inr_per_kwh"], provisional=False)
    return dict(mode="sim", source="SIMULATED", node_id="SN-SIM-01", now=res.get("ts") or 0, online=True, state=h["state"], status_key="st." + h["state"],
                profile=dict(id=prof["id"], name=prof["name"], kwp=prof.get("kwp")),
                badges=dict(voltage="SIMULATED", current="SIMULATED", light="SIMULATED", temperature="SIMULATED", estimate="SIMULATED",
                            money="SIMULATED", health="SIMULATED"),
                reading=dict(v=res["measured"]["vmp_v"], ma_raw=res["measured"]["imp_ma"], ma_cal=res["measured"]["imp_ma"],
                             p_mw=res["measured"]["pmax_mw"], state="sweep", live=True, settled=True, ts=res.get("ts")),
                sensors=dict(ldr=[], temperature=res["temp_c"], temperature_fitted=True, ldr_fitted=False, light_pct=res["light_pct"],
                             light_src="sim", current_pct=None, analysis=dict(shaded=[], pattern=None, have_ref=False, have_regions=False)),
                diagnosis=dx, health=dict(h, previous=None, trend=None), money=m, cards=cards, actions=acts,
                quality=None, loss_pct=res["loss_pct"],
                expected=dict(rows=[dict(key="isc", unit="mA", expected=res["expected"]["isc_ma"], measured=res["measured"]["isc_ma"], badge="SIMULATED"),
                                    dict(key="voc", unit="V", expected=res["expected"]["voc_v"], measured=res["measured"]["voc_v"], badge="SIMULATED"),
                                    dict(key="pmax", unit="mW", expected=res["expected"]["pmax_mw"], measured=res["measured"]["pmax_mw"], badge="SIMULATED")],
                              basis="twin", equivalent_light=None, equivalent_pct=None),
                twin=_twin_block(h["state"], res["light_pct"], prof, sim=simmod, caption="twin.cap.sim"),
                baseline=None, points=None, calibration=calib.load())
