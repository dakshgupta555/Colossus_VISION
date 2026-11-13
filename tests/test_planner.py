"""Planner tests on three farm sizes (small, medium, large) plus obstacle, budget and dead-zone cases."""
import copy
import math

import pytest

from sn import planner as P

BASE = P.load_config()


def cfg_for(acres, rows, cols, **kw):
    c = copy.deepcopy(BASE)
    c["farm"] = dict(mode="area", acres=acres, aspect=1.5)
    c["grid"] = dict(rows=rows, cols=cols)
    for k, v in kw.items():
        c[k].update(v) if isinstance(v, dict) else c.__setitem__(k, v)
    return c


FARMS = {"small": cfg_for(2, 5, 6), "medium": cfg_for(10, 10, 16), "large": cfg_for(40, 20, 30)}


def check_rules(cfg, p):
    cap, maxd = cfg["node"]["max_blocks"], cfg["node"]["max_dist_m"]
    g = p["geometry"]
    seen = {}
    for n in p["nodes"]:
        assert len(n["blocks"]) <= cap, "capacity"
        for r, c in n["blocks"]:
            assert (r, c) not in seen, "block assigned twice"
            seen[(r, c)] = n["id"]
            d = math.hypot(n["x"] - (c + 0.5) * g["bw"], n["y"] - (r + 0.5) * g["bh"])
            assert d <= maxd + 1e-6, "block beyond sensing distance"
    obs = {tuple(o) for o in cfg["obstacles"]}
    usable = {(r, c) for r in range(g["rows"]) for c in range(g["cols"])} - obs
    assert set(seen) == usable, "every usable block exactly once"
    assert not (set(seen) & obs), "no node monitors an obstacle"


@pytest.mark.parametrize("name", list(FARMS))
def test_optimised_rules_and_coverage(name):
    cfg = FARMS[name]
    p = P.plan(cfg)
    check_rules(cfg, p)
    assert p["coverage_pct"] == 100.0 and not p["dead_zones"]
    assert len(p["nodes"]) <= p["usable_blocks"]
    assert len(p["nodes"]) >= math.ceil(p["usable_blocks"] / cfg["node"]["max_blocks"])
    R = p["lora_range_m"]
    for n in p["nodes"]:
        assert any(l["node"] == n["id"] and l["distance_m"] <= R for l in p["links"]), "every node linked"


@pytest.mark.parametrize("name", list(FARMS))
def test_optimised_is_cheaper_than_dense(name):
    s = P.scenarios(FARMS[name])
    assert len(s["B"]["plan"]["nodes"]) < len(s["A"]["plan"]["nodes"])
    assert s["B"]["cost"]["capex"] < s["A"]["cost"]["capex"]
    assert s["A"]["plan"]["coverage_pct"] == 100.0
    assert s["C"]["cost"]["capex"] <= s["budget_inr"] or s["C"]["cost"]["nodes"] == 0


def test_obstacles_are_skipped():
    cfg = cfg_for(10, 10, 16, obstacles=[[r, c] for r in range(3, 6) for c in range(4, 9)])
    p = P.plan(cfg)
    check_rules(cfg, p)
    assert p["usable_blocks"] == 160 - 15


def test_tight_sensing_distance_adds_nodes():
    loose = P.plan(cfg_for(10, 10, 16, node=dict(max_blocks=8, max_dist_m=400)))
    tcfg = cfg_for(10, 10, 16, node=dict(max_blocks=8, max_dist_m=45))
    tight = P.plan(tcfg)
    check_rules(tcfg, tight)
    assert len(tight["nodes"]) > len(loose["nodes"])


def test_redundant_gateways_double_cover():
    cfg = cfg_for(40, 20, 30, gateways=dict(mode="auto", count=1, redundant=True), lora=dict(range_m=700, obstructed=False, obstructed_factor=0.55))
    p = P.plan(cfg)
    assert len(p["gateways"]) >= 2
    assert p["redundancy_pct"] > 50


def test_dead_zones_reported_with_fixed_gateways_and_obstructed_range():
    cfg = cfg_for(40, 20, 30, gateways=dict(mode="fixed", count=1, redundant=False), lora=dict(range_m=400, obstructed=True, obstructed_factor=0.5))
    p = P.plan(cfg)
    assert p["dead_zones"], "a single short-range gateway cannot reach a 40 acre farm"
    assert p["coverage_pct"] < 100.0
    assert all(d["distance_m"] > p["lora_range_m"] for d in p["dead_zones"])


def test_budget_scenario_loses_coverage():
    cfg = cfg_for(10, 10, 16, budget_inr=30000)
    s = P.scenarios(cfg)
    assert s["C"]["cost"]["capex"] <= 30000
    assert s["C"]["plan"]["coverage_pct"] < s["B"]["plan"]["coverage_pct"]
    assert not s["budget_assumed"]
