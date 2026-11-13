"""SIMULATED telemetry for a planned farm. Reuses the existing fleet simulation (pv_sim scenarios + engine.diagnose).

Every value is SIMULATED. A few neighbouring nodes get the same fault (a "zone") so the map shows patterns like a shaded or
dusty area, and zones are then DETECTED from the diagnosed results (adjacent faulty nodes of the same light-loss family),
not read back from what was injected.
"""
import numpy as np

from . import pv_sim as S

LIGHT_FAMILY = {"soiling", "partial_shade"}
WEIGHTS = dict(healthy=0.86, soiling=0.04, partial_shade=0.03, series_resistance=0.04, bypass_short=0.03)
MAX_NODES = 200


def simulate(plan, cfg, engine, seed, yld, tariff):
    rng = np.random.default_rng(seed)
    nodes = plan["nodes"][:MAX_NODES]
    n = len(nodes)
    if n == 0:
        return dict(nodes={}, zones=[], seed=seed)
    xy = np.array([[a["x"], a["y"]] for a in nodes])
    D = np.sqrt(((xy[:, None] - xy[None]) ** 2).sum(-1))
    faults = [str(rng.choice(list(WEIGHTS), p=list(WEIGHTS.values()))) for _ in range(n)]
    injected = []
    for _ in range(min(2, max(1, n // 12))):                      # one or two zones of neighbouring nodes
        seed_i = int(rng.integers(n))
        zone = [int(i) for i in np.argsort(D[seed_i])[: int(rng.integers(4, 8))]]
        kind = str(rng.choice(["soiling", "partial_shade"]))
        for i in zone:
            faults[i] = kind
        injected.append(zone)
    offline = {int(i) for i in rng.choice(n, size=min(n, int(rng.random() < 0.7) * 1 + 0), replace=False)} if n > 3 else set()
    kwp_block = cfg.get("kwp_per_block", 0.5)
    out = {}
    for i, a in enumerate(nodes):
        if i in offline:
            out[a["id"]] = dict(status="offline", label=None, loss_pct=None, inr_day=None, conf=None, minutes_ago=int(rng.integers(30, 400)))
            continue
        s = S.make_scenario(faults[i], rng, hard=bool(rng.random() < 0.4))
        s["g"] = float(rng.uniform(0.7, 1.0))
        payload = S.simulate(s, rng)
        payload.update(node_id=a["id"], source="sim", seq=0, fw="farm-sim")
        res = engine.diagnose(payload, kwp=max(0.1, len(a["blocks"]) * kwp_block), yld=yld, tariff=tariff)
        faulty = res["label"] != "healthy" or res["status"] != "ok"
        out[a["id"]] = dict(status=("bad" if res["loss_pct"] >= 20 else "warn") if faulty else "ok", label=res["label"], loss_pct=res["loss_pct"],
                            inr_day=res["money"]["inr_day"], conf=res["confidence"], minutes_ago=int(rng.integers(5, 900)) if faulty else 0)
    return dict(nodes=out, zones=detect_zones(nodes, out, xy, D), seed=seed)


def detect_zones(nodes, status, xy, D):
    """Zone issue: >= 3 adjacent nodes whose diagnosis is in the light-loss family (soiling / partial shade)."""
    n = len(nodes)
    if n < 3:
        return []
    nn = np.sort(D + np.eye(n) * 1e9, axis=1)[:, 0]
    link = 1.8 * float(np.median(nn))
    hit = [i for i in range(n) if status[nodes[i]["id"]].get("label") in LIGHT_FAMILY and status[nodes[i]["id"]]["status"] != "ok"]
    seen, zones = set(), []
    for s in hit:
        if s in seen:
            continue
        comp, stack = [], [s]
        seen.add(s)
        while stack:
            u = stack.pop()
            comp.append(u)
            for v in hit:
                if v not in seen and D[u][v] <= link:
                    seen.add(v)
                    stack.append(v)
        if len(comp) >= 3:
            ids = [nodes[i]["id"] for i in comp]
            blocks = sum(len(nodes[i]["blocks"]) for i in comp)
            loss = float(np.mean([status[x]["loss_pct"] for x in ids]))
            zones.append(dict(id="Z%d" % (len(zones) + 1), nodes=ids, n_blocks=blocks, loss_pct=round(loss, 1),
                              inr_day=round(sum(status[x]["inr_day"] for x in ids), 1), minutes_ago=int(min(status[x]["minutes_ago"] for x in ids)),
                              family=str(status[ids[0]]["label"])))
    return zones
