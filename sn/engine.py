"""Diagnostic engine: curve in -> explained diagnosis out.

Every sentence is returned twice: as English text (headline, evidence, action) and as a
language-neutral {key, p} pair (verdict, ev, act, location) that sn/i18n.py or the web
console can render in any language with the same measured numbers.
"""
import numpy as np

from . import config as C
from . import features as F
from . import i18n as L
from .model import load_or_train
from .rules import rule_diagnose

_BUNDLE = None
PANEL_NAMES = ["A", "B"]          # A is the panel on the ground side of the string


def bundle():
    global _BUNDLE
    if _BUNDLE is None:
        _BUNDLE = load_or_train()
    return _BUNDLE


def _pct(x):
    return int(round(x * 100))


def locate(payload, label, status):
    """Where in the string the problem is.

    The I-V curve alone says WHAT is wrong and how many panels are affected, but not
    which panel. If the node also sends mid_v (voltage across panel A at every sweep
    point, from a divider on the junction between the panels) the panel is pinpointed.
    """
    if status != "ok":
        return dict(kind="unknown", panel=None, basis=None)
    if label == "healthy":
        return dict(kind="none", panel=None, basis=None)
    if label == "soiling":
        return dict(kind="all", panel=None, basis="curve")
    if label == "series_resistance":
        return dict(kind="joint", panel=None, basis="curve")
    panel = None
    mid = payload.get("mid_v")
    pts = np.asarray(payload["points"], float)
    if mid is not None and len(mid) == len(pts):
        va = np.asarray(mid, float)
        v, i = pts[:, 0], pts[:, 1]
        vb = v - va
        if label == "partial_shade":
            hi = i >= 0.9 * i.max()            # above the shaded panel's current: it is bypassed
            if hi.sum() >= 3 and abs(va[hi].mean() - vb[hi].mean()) > 0.15 * max(v.max(), 1e-6):
                panel = "A" if va[hi].mean() < vb[hi].mean() else "B"
        else:                                  # bypass_short: that panel sits at ~0 V everywhere
            up = v >= 0.5 * v.max()
            share = va[up].mean() / max(v[up].mean(), 1e-6)
            panel = "A" if share < 0.25 else ("B" if share > 0.75 else None)
    if label == "partial_shade":
        kind = "panel" if panel else "one_panel"
    else:
        kind = "diode" if panel else "diode_one"
    return dict(kind=kind, panel=panel, basis="midpoint" if panel else "curve")


def _where(loc, fallback="w.one"):
    return dict(key="w.panel", p=dict(x=loc["panel"])) if loc.get("panel") else dict(key=fallback, p={})


def evidence_items(label, f, loc):
    ev = []
    isc, voc, ff = _pct(f["isc_ratio"]), _pct(f["voc_ratio"]), _pct(f["ff_ratio"])
    if label == "bypass_short":
        ev.append(dict(key="e.bypass_voc", p=dict(voc=voc, isc=isc)))
        ev.append(dict(key="e.bypass_shape", p={}))
    elif label == "partial_shade":
        ev.append(dict(key="e.shade_step", p=dict(step=f"{f['step_score']:.1f}")))
        if f["n_peaks"] >= 2:
            ev.append(dict(key="e.shade_peaks", p={}))
        ev.append(dict(key="e.shade_isc", p=dict(isc=isc)))
    elif label == "series_resistance":
        ev.append(dict(key="e.rs_slope", p=dict(rs=f"{f['rs_ratio']:.1f}")))
        ev.append(dict(key="e.rs_ff", p=dict(ff=ff, isc=isc, voc=voc)))
    elif label == "soiling":
        ev.append(dict(key="e.soil_isc", p=dict(isc=isc)))
        ev.append(dict(key="e.soil_shape", p=dict(ff=ff)))
    else:
        ev.append(dict(key="e.ok", p=dict(pmax=_pct(f["pmax_ratio"]), light=_pct(f["g_est"]),
                                          temp=f"{f['temp_c']:.0f}")))
        if f["g_est"] < 0.4:
            ev.append(dict(key="e.ok_dim", p={}))
    if loc.get("basis") == "midpoint":
        ev.append(dict(key="e.loc_mid", p=dict(x=loc["panel"])))
    return ev


def money(loss, kwp, yld, tariff):
    kwh_day = loss * kwp * yld
    return dict(kwh_day=round(kwh_day, 2), inr_day=round(kwh_day * tariff, 1),
                inr_year=round(kwh_day * tariff * 365))


def diagnose(payload, kwp=C.DEFAULT_KWP, yld=C.DEFAULT_YIELD, tariff=C.DEFAULT_TARIFF, panel=None):
    b = bundle()
    f, m, e, e_pts = F.extract(payload, panel=panel)
    x = np.array([F.vector(f)])
    proba = b["clf"].predict_proba(x)[0]
    classes = list(b["clf"].classes_)
    k = int(np.argmax(proba))
    label, conf = classes[k], float(proba[k])
    rule = rule_diagnose(f)
    ood = bool(b["iso"].score_samples(x)[0] < b["iso_thr"])
    if ood:
        status = "unknown"
    elif conf < C.MIN_CONFIDENCE:
        status = "uncertain"
    else:
        status = "ok"
    loss = max(0.0, 1.0 - m["pmax"] / e["pmax"])
    if label == "healthy" and status == "ok":
        loss = 0.0 if loss < 0.03 else loss
    loss_pct = round(loss * 100, 1)

    loc = locate(payload, label, status)
    location = dict(loc, key="loc." + loc["kind"], p=dict(x=loc["panel"]) if loc["panel"] else {})
    fault_name = dict(key="f." + label, p={})
    if status == "unknown":
        verdict, act = dict(key="v.unknown", p={}), dict(key="a.unknown", p={})
        headline = "Unknown condition: send a technician"
    elif status == "uncertain":
        verdict, act = dict(key="v.uncertain", p=dict(fault=fault_name)), dict(key="a.uncertain", p=dict(fault=fault_name))
        headline = f"Uncertain: possibly {C.NICE[label]}"
    else:
        verdict = dict(key="v." + label, p=dict(loss=int(round(loss_pct)), where=_where(loc)))
        act = dict(key="a." + label, p=dict(where=_where(loc, "w.affected")))
        headline = C.NICE[label]
    ev = evidence_items(label, f, loc)
    return dict(
        label=label, headline=headline, status=status, confidence=round(conf, 3),
        probs={c: round(float(p), 3) for c, p in zip(classes, proba)},
        rule_label=rule, rules_agree=(rule == label), out_of_distribution=ood,
        verdict=verdict, location=location, act=act, ev=ev,
        summary=L.t("en", verdict["key"], verdict["p"]),
        evidence=[L.t("en", i["key"], i["p"]) for i in ev], action=L.t("en", act["key"], act["p"]),
        measured=dict(isc_ma=round(m["isc"] * 1000, 1), voc_v=round(m["voc"], 3),
                      pmax_mw=round(m["pmax"] * 1000, 1), ff=round(m["ff"], 3),
                      vmp_v=round(m["vmp"], 3), imp_ma=round(m["imp"] * 1000, 1)),
        expected=dict(isc_ma=round(e["isc"] * 1000, 1), voc_v=round(e["voc"], 3),
                      pmax_mw=round(e["pmax"] * 1000, 1), ff=round(e["ff"], 3)),
        loss_pct=loss_pct, loss_mw=round(max(0.0, e["pmax"] - m["pmax"]) * 1000, 1),
        money=money(loss, kwp, yld, tariff),
        assumptions=dict(kwp=kwp, yield_kwh_per_kwp_day=yld, tariff_inr_per_kwh=tariff),
        light_pct=round(f["g_est"] * 100, 1), temp_c=round(f["temp_c"], 1),
        features={k2: round(float(v), 4) for k2, v in f.items()},
        expected_points=e_pts,
    )
