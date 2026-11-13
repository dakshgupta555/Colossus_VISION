"""Probabilistic diagnosis for REAL bench data (one panel: Isc, Voc, optional reference LDR).

What it is: a small calibrated Random Forest (same pattern as sn/model.py) trained on pvlib physics-twin
simulations of our single panel, using ONLY what the node measures:
    isc_ratio  = Isc now / Isc at baseline          voc_ratio = Voc now / Voc at baseline
    isc0_ma    = baseline Isc (signal level: sets how noisy the ratios are)
    ldr_ratio  = reference LDR now / at baseline    (second model, only when a reference LDR reports)
with noise that matches the INA219 (0.1 mA steps, 3 s averages). Priors are the assumption in PRIORS below. Dust, shading and a light change look almost the same in Isc/Voc, so their probabilities come out
SPREAD; nothing here forces them apart. The reference LDR is what separates a light change from a cover
on the panel.

Not measured, therefore not claimed: no accuracy figure is produced for real data (needs held-out real sessions).
A loose joint of a few ohms is invisible in Isc/Voc at ~1 mA (it needs a sweep), so the series-resistance
class only covers very high resistance. The bypass diode is not applicable: this panel has none.

The time pattern of the Isc readings then adjusts the probabilities with the likelihood ratios in
TEMPORAL_LR below. Those numbers are ASSUMPTIONS, not fitted to field data.
"""
import json
import os

import joblib
import numpy as np
from pvlib import pvsystem
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier

from . import bench

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_PATH = os.path.join(ROOT, "models", "realdx.joblib")
CLASSES = ["healthy", "soiling", "partial_shade", "series_resistance", "light_change"]

# ---- assumptions, all in one place -------------------------------------------------------------
TEMPORAL_LR = {   # likelihood ratio of each cause given how the Isc moved (ASSUMPTIONS, not fitted)
    "transient":    dict(healthy=1.0, soiling=0.15, partial_shade=2.0, series_resistance=0.7, light_change=2.0),
    "gradual":      dict(healthy=1.0, soiling=2.0, partial_shade=0.6, series_resistance=0.8, light_change=1.0),
    "sudden_stays": dict(healthy=0.5, soiling=0.5, partial_shade=1.5, series_resistance=1.5, light_change=1.5),
    "stable":       dict(healthy=1.0, soiling=1.0, partial_shade=1.0, series_resistance=1.0, light_change=1.0),
    "unknown":      dict(healthy=1.0, soiling=1.0, partial_shade=1.0, series_resistance=1.0, light_change=1.0),
}
# Prior plausibility of each cause (ASSUMPTION). The model is trained on balanced classes (equal priors); these
# re-weight it. A joint resistance big enough to move Isc at ~1 mA (kilo-ohms) is rare, so it is kept small.
PRIORS = dict(healthy=0.30, soiling=0.25, partial_shade=0.20, series_resistance=0.05, light_change=0.20)
PATTERN = dict(min_samples=20, min_span_s=30, dip=0.85, recover=0.07, step=0.15, gradual_drop=0.05, gradual_span_s=120)
CERTAINTY = dict(high=0.35, moderate=0.20, low=0.08)      # margin between the top two probabilities
RANGE = dict(isc_ratio=(0.15, 2.0), voc_ratio=(0.7, 1.3))  # outside this the model has not seen the case
SIM_NOISE = dict(ma_read=0.08, step_ma=0.1, n_avg=3, v=0.015, ldr=0.05)   # INA219 / ADC noise used in training
# Unmodelled drift added to EVERY class so Isc/Voc cannot separate causes more sharply than reality does
# (torch moved, panel warmed by the torch, spectrum, contacts). ASSUMPTIONS, not fitted to field data.
DRIFT = dict(isc=0.03, voc=0.012)

RULE_CLAIMS = {"finding.ok": {"healthy"}, "finding.light": {"soiling", "partial_shade", "light_change"},
               "finding.voltage": {"partial_shade"}, "finding.unclear": None}


# ---- simulation of the single panel ------------------------------------------------------------
def _meas_isc(true_ma, rng):
    reads = np.round((true_ma + rng.normal(0, SIM_NOISE["ma_read"], SIM_NOISE["n_avg"])) / SIM_NOISE["step_ma"]) * SIM_NOISE["step_ma"]
    return float(np.mean(reads))


def _isc_with_rs(il_a, voc, rs):
    a = bench._knee_ratio() * voc
    i0 = (il_a - voc / bench.RSH) / np.expm1(voc / a)
    return float(pvsystem.i_from_v(0.0, il_a, i0, rs, bench.RSH, a, method="lambertw"))


def sample(cls, rng):
    """One simulated (baseline, now) pair for a fault class -> feature dict. Physics, then measurement noise."""
    r = bench._knee_ratio()                     # Voc ~ 1 + r * ln(light scale)
    isc0 = rng.uniform(0.6, 4.0)                # mA at baseline (the torch)
    voc0 = rng.uniform(0.5, 4.0)
    scale, voc_ratio_true, gamma = 1.0, 1.0, rng.uniform(0.5, 1.0)
    ldr_scale = 1.0
    if cls == "healthy":
        scale = rng.normal(1.0, 0.02); voc_ratio_true = rng.normal(1.0, 0.008)
    elif cls == "soiling":
        scale = 1.0 - rng.uniform(0.08, 0.6); voc_ratio_true = 1 + r * np.log(scale)
    elif cls == "light_change":
        scale = rng.uniform(0.3, 0.92) if rng.random() < 0.5 else rng.uniform(1.08, 1.6)
        voc_ratio_true = 1 + r * np.log(scale); ldr_scale = scale ** gamma
    elif cls == "partial_shade":
        depth, share = rng.uniform(0.2, 0.9), rng.uniform(0.1, 0.5)   # shade depth, share of cells covered
        scale = 1.0 - depth; voc_ratio_true = 1 + share * r * np.log(1.0 - depth)
    elif cls == "series_resistance":
        # A few ohms of joint resistance does NOT change Isc or Voc at ~1 mA (it only softens the knee, which needs a
        # sweep). Isc moves only when Rs is comparable to Voc/Isc (kilo-ohms), so that is the only case trained here.
        rs = rng.uniform(0.85, 1.8) * voc0 / (isc0 / 1000.0)
        scale = _isc_with_rs(isc0 / 1000.0, voc0, rs) / (isc0 / 1000.0)
    scale *= rng.normal(1.0, DRIFT["isc"]); voc_ratio_true *= rng.normal(1.0, DRIFT["voc"])
    isc_b, isc_n = _meas_isc(isc0, rng), _meas_isc(isc0 * scale, rng)
    voc_b = voc0 + rng.normal(0, SIM_NOISE["v"]); voc_n = voc0 * voc_ratio_true + rng.normal(0, SIM_NOISE["v"])
    return dict(isc_ratio=isc_n / max(isc_b, 0.1), voc_ratio=voc_n / voc_b, isc0_ma=max(isc_b, 0.1),
                ldr_ratio=ldr_scale * rng.normal(1.0, SIM_NOISE["ldr"]))


def _vec(f, with_ldr):
    v = [f["isc_ratio"], f["voc_ratio"], np.log(f["isc0_ma"])]
    return v + [f["ldr_ratio"]] if with_ldr else v


def train(n_per_class=3000, seed=21, verbose=True):
    rng = np.random.default_rng(seed)
    rows = [(c, sample(c, rng)) for c in CLASSES for _ in range(n_per_class)]
    y = np.array([c for c, _ in rows])
    models = {}
    for with_ldr in (False, True):
        X = np.array([_vec(f, with_ldr) for _, f in rows])
        rf = RandomForestClassifier(n_estimators=200, min_samples_leaf=15, class_weight="balanced", n_jobs=-1, random_state=0)
        models[with_ldr] = CalibratedClassifierCV(rf, method="sigmoid", cv=3).fit(X, y)
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    joblib.dump(dict(models=models, n_per_class=n_per_class), MODEL_PATH)
    if verbose:
        print(f"real-bench classifier trained on {len(y)} physics-twin simulations (no accuracy claimed for real data)")
    return models


_cache = {}


def models():
    if "m" not in _cache:
        if not os.path.exists(MODEL_PATH):
            train()
        _cache["m"] = joblib.load(MODEL_PATH)["models"]
    return _cache["m"]


# ---- time pattern -----------------------------------------------------------------------------
def pattern(series):
    """series = [(ts, isc_ratio), ...] from SHORT-state readings. -> (name, detail)."""
    P = PATTERN
    if len(series) < P["min_samples"] or series[-1][0] - series[0][0] < P["min_span_s"]:
        return "unknown", {}
    t = np.array([s[0] for s in series]); y = np.array([s[1] for s in series])
    k = 5
    ys = np.array([np.median(y[max(0, i - k // 2): i + k // 2 + 1]) for i in range(len(y))])
    now, early = float(np.median(ys[-10:])), float(np.median(ys[:10]))
    if abs(now - 1.0) < P["recover"] and ys.min() < P["dip"]:
        return "transient", dict(low=float(ys.min()))
    if now < 1.0 - P["recover"]:
        step = max((ys[i] - ys[min(i + k, len(ys) - 1)] for i in range(len(ys))), default=0.0)
        if step > P["step"]:
            return "sudden_stays", dict(step=float(step))
        if early - now > P["gradual_drop"] and t[-1] - t[0] >= P["gradual_span_s"] and np.all(np.diff(ys[::max(1, len(ys) // 20)]) < 0.03):
            return "gradual", dict(drop=float(early - now))
    return "stable", {}


# ---- evidence, "what would tell them apart", certainty -----------------------------------------
def _certainty(probs, agree, in_range, quality):
    """Diagnostic certainty from the margin between the top two probabilities, whether the physics rules agree
    (None = the rules make no claim), the out-of-range check and data quality. Separate from the health score."""
    order = sorted(probs, key=probs.get, reverse=True)
    margin = probs[order[0]] - probs[order[1]]
    lvl = 3 if margin >= CERTAINTY["high"] else 2 if margin >= CERTAINTY["moderate"] else 1 if margin >= CERTAINTY["low"] else 0
    if agree is False:
        lvl -= 1
    if quality is not None and quality < 50:
        lvl -= 1
    if not in_range:
        lvl = 0
    lvl = max(0, min(3, lvl))
    return dict(level=["insufficient", "low", "moderate", "high"][lvl], margin=round(margin, 3),
                rule_agree=agree, in_range=in_range, quality=quality)


def _tell_apart(order, probs, have_ref, have_regions):
    keys, top2 = [], set(order[:3])
    if top2 & {"soiling", "partial_shade"}:
        keys.append("ta.sweep")
        keys.append("ta.clean")
    if "light_change" in top2 and not have_ref:
        keys.append("ta.ldr")
    if "partial_shade" in top2 and not have_regions:
        keys.append("ta.regions")
    if "series_resistance" in top2:
        keys.append("ta.joint")
    if {"soiling", "partial_shade", "light_change"} & top2:
        keys.append("ta.time")
    return list(dict.fromkeys(keys))


def diagnose(isc_ratio, voc_ratio, isc0_ma, ldr_ratio=None, pat="unknown", pat_detail=None, rule_key=None,
             quality=None, ldr_info=None):
    """Probabilities, ranking, evidence, 'what would tell them apart' and the certainty badge."""
    have_ldr = ldr_ratio is not None
    f = dict(isc_ratio=isc_ratio, voc_ratio=voc_ratio, isc0_ma=max(isc0_ma, 0.1), ldr_ratio=ldr_ratio or 1.0)
    m = models()[have_ldr]
    base = dict(zip(m.classes_, m.predict_proba([_vec(f, have_ldr)])[0]))
    lr = TEMPORAL_LR.get(pat, TEMPORAL_LR["unknown"])
    adj = {c: base[c] * (PRIORS[c] / 0.2) * lr[c] for c in CLASSES}
    z = sum(adj.values()) or 1.0
    probs = {c: adj[c] / z for c in CLASSES}
    order = sorted(CLASSES, key=probs.get, reverse=True)
    in_range = (RANGE["isc_ratio"][0] <= isc_ratio <= RANGE["isc_ratio"][1]
                and RANGE["voc_ratio"][0] <= voc_ratio <= RANGE["voc_ratio"][1])
    isc_pct, voc_pct = (isc_ratio - 1) * 100, (voc_ratio - 1) * 100
    extra = []
    if have_ldr:
        extra.append(dict(key="ev.ldr.same" if ldr_ratio >= 0.9 else "ev.ldr.down", p=dict(x=round(ldr_ratio * 100))))
    if ldr_info and ldr_info.get("pattern"):
        extra.append(dict(key="ev.cover." + ldr_info["pattern"], p={}))
    if pat in ("transient", "gradual", "sudden_stays"):
        extra.append(dict(key="ev.time." + pat, p={}))
    p = dict(isc=round(isc_pct, 1), voc=round(voc_pct, 1), isc_pct=round(isc_ratio * 100), voc_pct=round(voc_ratio * 100))
    evidence = {c: [dict(key="ev." + c, p=p)] + extra for c in CLASSES}
    return dict(probs={c: round(float(probs[c]), 4) for c in CLASSES}, base_probs={c: round(float(base[c]), 4) for c in CLASSES},
                order=order, top=order[0], others=[c for c in order[1:] if probs[c] >= 0.10],
                evidence=evidence, tell_apart=_tell_apart(order, probs, have_ldr, bool(ldr_info and ldr_info.get("have_regions"))),
                certainty=_certainty(probs, None if RULE_CLAIMS.get(rule_key) is None else (order[0] in RULE_CLAIMS[rule_key]), in_range, quality),
                features=dict(isc_ratio=round(isc_ratio, 4), voc_ratio=round(voc_ratio, 4), isc0_ma=round(isc0_ma, 3),
                              ldr_ratio=None if ldr_ratio is None else round(ldr_ratio, 4)),
                pattern=pat, pattern_detail=pat_detail or {}, uses_ldr=have_ldr,
                bypass="not_applicable", temporal_lr=lr, priors=PRIORS)


if __name__ == "__main__":
    train()
    rng = np.random.default_rng(5)
    show = lambda d: {k[:5]: round(v, 2) for k, v in d["probs"].items()}
    for c in CLASSES:
        f = sample(c, rng)
        d = diagnose(f["isc_ratio"], f["voc_ratio"], f["isc0_ma"], f["ldr_ratio"])
        print(f"{c:18s} isc {f['isc_ratio']:.2f} voc {f['voc_ratio']:.3f} ldr {f['ldr_ratio']:.2f} ->", show(d), d["certainty"]["level"])
    print("same Isc/Voc, no LDR:", show(diagnose(0.70, 0.985, 1.0)), diagnose(0.70, 0.985, 1.0)["certainty"]["level"])
    print("same Isc/Voc, LDR steady:", show(diagnose(0.70, 0.985, 1.0, 1.0)))
    print("same Isc/Voc, LDR also 0.7:", show(diagnose(0.70, 0.985, 1.0, 0.70)))
    print("healthy 0.99/1.0:", show(diagnose(0.99, 1.0, 1.0)), diagnose(0.99, 1.0, 1.0)["certainty"]["level"])
