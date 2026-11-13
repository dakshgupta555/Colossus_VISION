"""Health score 0-100: ONE place for the weights and thresholds.

score = WEIGHTS.output * output_score + WEIGHTS.electrical * electrical_score
  output_score      how much of the expected output is there (vs the baseline for real data, vs the twin for sweeps)
  electrical_score  deviations that are not just "less light": Voc sag, fill-factor loss, series-resistance
                    signature, a stepped curve
Data quality and diagnostic certainty are separate meters and are NOT mixed into the score.
Real data (Isc and Voc only) is always PROVISIONAL. No data = no score (never "healthy" by default).
"""
WEIGHTS = dict(output=0.80, electrical=0.20)
THRESHOLDS = dict(healthy=85, attention=60)        # score >= 85 healthy, >= 60 attention, below that critical
VOC_TOLERANCE = 0.03                               # a Voc sag smaller than this is not penalised
QUALITY_WEIGHTS = dict(rate=0.30, noise=0.30, calibration=0.25, online=0.15)


HYSTERESIS = 3     # points: getting worse is shown at once, getting better needs this much margin (stops flicker)


def state_hyst(score, prev):
    """state_of() with one-sided hysteresis against the previously committed state."""
    raw = state_of(score)
    rank = {"critical": 0, "attention": 1, "healthy": 2}
    if prev not in rank or raw not in rank or rank[raw] <= rank[prev]:
        return raw
    need = THRESHOLDS["healthy"] if raw == "healthy" else THRESHOLDS["attention"]
    return raw if score >= need + HYSTERESIS else prev


def state_of(score):
    if score is None:
        return "nodata"
    return "healthy" if score >= THRESHOLDS["healthy"] else "attention" if score >= THRESHOLDS["attention"] else "critical"


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _finish(output_score, electrical_score, parts, provisional):
    score = WEIGHTS["output"] * output_score + WEIGHTS["electrical"] * electrical_score
    factors = sorted([f for f in parts if f["points"] >= 0.5], key=lambda f: -f["points"])[:3]
    return dict(score=round(_clamp(score)), state=state_of(score), factors=factors, provisional=provisional,
                output_score=round(output_score, 1), electrical_score=round(electrical_score, 1))


def real(isc_ratio, voc_ratio):
    """From the measured Isc and Voc against the healthy baseline."""
    loss_isc, loss_voc = max(0.0, 1 - isc_ratio), max(0.0, 1 - voc_ratio)
    output = 100 * (1 - min(1.0, loss_isc + loss_voc))
    elec = 100 - 400 * max(0.0, loss_voc - VOC_TOLERANCE)
    wo, we = WEIGHTS["output"], WEIGHTS["electrical"]
    parts = [dict(key="hf.isc", points=wo * 100 * loss_isc, p=dict(x=round(loss_isc * 100))),
             dict(key="hf.voc", points=wo * 100 * loss_voc, p=dict(x=round(loss_voc * 100))),
             dict(key="hf.volt", points=we * (100 - _clamp(elec)), p=dict(x=round(max(0, loss_voc - VOC_TOLERANCE) * 100)))]
    return _finish(output, _clamp(elec), parts, provisional=True)


def sweep(res):
    """From a full-sweep diagnosis result (engine.diagnose): output against the twin plus electrical signatures."""
    f = res["features"]
    loss = res["loss_pct"] / 100.0
    output = 100 * (1 - loss)
    voc_dev = max(0.0, 1 - f.get("voc_ratio", 1.0) - VOC_TOLERANCE)
    ff_dev = max(0.0, 1 - f.get("ff_ratio", 1.0) - 0.05)
    rs_dev = max(0.0, f.get("rs_ratio", 1.0) - 1.3)
    step = 25.0 if f.get("n_peaks", 1) > 1 else 0.0
    pens = [("hf.volt", 400 * voc_dev, dict(x=round(voc_dev * 100))), ("hf.fill", 300 * ff_dev, dict(x=round(ff_dev * 100))),
            ("hf.series", min(40.0, 40 * rs_dev), {}), ("hf.step", step, {})]
    elec = 100 - sum(p for _, p, _ in pens)
    wo, we = WEIGHTS["output"], WEIGHTS["electrical"]
    parts = [dict(key="hf.output", points=wo * (100 - output), p=dict(x=round(loss * 100)))]
    parts += [dict(key=k, points=we * v, p=p) for k, v, p in pens]
    return _finish(output, _clamp(elec), parts, provisional=False)


def data_quality(rate, spread, cal_status, online):
    """0-100 from readings per second, current noise, calibration status and sensor connection (separate meter)."""
    if not online:
        return dict(score=0, parts=dict(rate=0, noise=0, calibration=0, online=0))
    q_rate = _clamp(100 * (rate or 0) / 0.9)                       # about 1 reading a second is full marks
    q_noise = 100.0 if spread is None else _clamp(100 * (1 - max(0.0, spread - 0.1) / 0.4))   # 0.1 mA = one INA219 step
    q_cal = 100.0 if cal_status == "VERIFIED" else 65.0
    parts = dict(rate=q_rate, noise=q_noise, calibration=q_cal, online=100.0)
    score = sum(QUALITY_WEIGHTS[k] * v for k, v in parts.items())
    return dict(score=round(score), parts={k: round(v) for k, v in parts.items()})


def trend(history, now_score, window_s=60, now_ts=None):
    """(previous score about a minute ago, arrow) from a list of (ts, score)."""
    if now_score is None or not history:
        return None, None
    t_ref = (now_ts or history[-1][0]) - window_s
    prev = next((s for ts, s in reversed(history) if ts <= t_ref and s is not None), None)
    if prev is None:
        prev = next((s for ts, s in history if s is not None), None)
    if prev is None:
        return None, None
    d = now_score - prev
    return prev, ("up" if d > 2 else "down" if d < -2 else "flat")
