"""Maintenance priority score: weighted geometric mean of five factors, categories from the config."""
from sn import maint, planner as P

CFG = P.load_config()


def item(**kw):
    base = dict(kind="soiling", source="SIMULATED", node="X", severity=0.5, minutes_ago=60, inr_day=10, evidence_strength=0.8, loss_pct=20)
    base.update(kw)
    return base


def test_score_in_range_and_monotonic():
    lo = maint.score(item(severity=0.1, inr_day=1, minutes_ago=5), 1, CFG)[0]
    hi = maint.score(item(severity=1.0, inr_day=60, minutes_ago=2000), 3, CFG)[0]
    assert 0 <= lo < hi <= 100


def test_recurrence_and_duration_raise_priority():
    a = maint.score(item(), 1, CFG)[0]
    assert maint.score(item(), 4, CFG)[0] > a
    assert maint.score(item(minutes_ago=3000), 1, CFG)[0] > a


def test_categories_follow_thresholds():
    assert maint.score(item(severity=1, inr_day=60, minutes_ago=3000, evidence_strength=1), 4, CFG)[1] == "critical"
    assert maint.score(item(severity=0.1, inr_day=1, minutes_ago=1, evidence_strength=0.3), 1, CFG)[1] == "low"


def test_real_items_are_phrased_as_possible_causes():
    c = maint.card(item(kind="live_red", source="REAL", node="SN-001", avg_ma=0.05))
    assert c["why_head"] == "maint.why.real"
    assert any(e["key"] == "maint.ev.sensor" for e in c["evidence"])
    assert maint.card(item())["why_head"] == "maint.why.sim"
