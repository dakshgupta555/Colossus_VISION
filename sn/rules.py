"""Physics-rule baseline. Transparent, needs no data; also used as a guardrail."""


def rule_diagnose(f):
    if f["voc_ratio"] < 0.72 and f["isc_ratio"] > 0.8:
        return "bypass_short"
    if f["n_peaks"] >= 2 or f["step_score"] > 2.5:
        return "partial_shade"
    if (f["rs_ratio"] > 1.12 or f["ff_ratio"] < 0.94) and f["isc_ratio"] > 0.93 and f["voc_ratio"] > 0.97:
        return "series_resistance"
    if f["isc_ratio"] < 0.90:
        return "soiling"
    return "healthy"
