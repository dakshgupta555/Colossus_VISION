"""Solar farm planner (PLANNING / SIMULATED): where to put sensing nodes and LoRa gateways. numpy only.

Everything here is a schematic layout on a block grid, not GPS, and every number comes from illustrative
assumptions in data/farm_planner.json. Rules enforced by plan():
  - every usable block is assigned to exactly one node (unless a budget scenario leaves some unmonitored)
  - a node monitors at most max_blocks blocks, all within max_dist_m of the node
  - every node is within LoRa range of a gateway (two in redundant mode), otherwise it is reported as a dead zone
"""
import json
import math
import os

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.environ.get("SN_FARM_CFG") or os.path.join(ROOT, "data", "farm_planner.json")
ACRE_M2 = 4046.856
MAX_BLOCKS_TOTAL = 900


def load_config(path=None):
    try:
        return json.load(open(path or PATH))
    except (OSError, ValueError):
        return {}


def save_config(cfg, path=None):
    json.dump(cfg, open(path or PATH, "w"), indent=1)


def geometry(cfg):
    """length, width (m), acres, block width/height (m)."""
    f, g = cfg["farm"], cfg["grid"]
    if f.get("mode", "area") == "area":
        area = float(f["acres"]) * ACRE_M2
        length = math.sqrt(area * float(f.get("aspect", 1.6)))
        width = area / length
    else:
        length, width = float(f["length_m"]), float(f["width_m"])
    rows, cols = int(g["rows"]), int(g["cols"])
    return dict(length_m=length, width_m=width, acres=length * width / ACRE_M2, rows=rows, cols=cols,
                bw=length / cols, bh=width / rows)


def lora_range(cfg):
    L = cfg["lora"]
    return float(L["range_m"]) * (float(L.get("obstructed_factor", 0.55)) if L.get("obstructed") else 1.0)


def blocks(cfg):
    """usable block list [(r,c)], centres Nx2 in metres, and the obstacle set."""
    g = geometry(cfg)
    obs = {tuple(o) for o in cfg.get("obstacles", [])}
    rc = [(r, c) for r in range(g["rows"]) for c in range(g["cols"]) if (r, c) not in obs]
    xy = np.array([[(c + 0.5) * g["bw"], (r + 0.5) * g["bh"]] for r, c in rc]) if rc else np.zeros((0, 2))
    return g, rc, xy, obs


def _dist(a, b):
    return np.sqrt(((a[:, None, :] - b[None, :, :]) ** 2).sum(-1))


def _assign(D, meds, cap):
    """Capacitated assignment by regret: blocks that would lose most by not getting their nearest node go first."""
    n, k = D.shape[0], len(meds)
    d = D[:, meds]
    order = np.argsort(d, axis=1)
    regret = d[np.arange(n), order[:, 1]] - d[np.arange(n), order[:, 0]] if k > 1 else np.zeros(n)
    load = np.zeros(k, int)
    asg = -np.ones(n, int)
    for j, m in enumerate(meds):          # a node's own block always belongs to it
        asg[m] = j
        load[j] += 1
    for i in np.argsort(-regret, kind="stable"):
        if asg[i] >= 0:
            continue
        for j in order[i]:
            if load[j] < cap:
                asg[i] = j
                load[j] += 1
                break
    return asg


def _init(D, xy, k):
    """Farthest-first start: first the block nearest the centroid, then repeatedly the block farthest from all picks."""
    meds = [int(np.argmin(((xy - xy.mean(0)) ** 2).sum(1)))]
    while len(meds) < k:
        meds.append(int(np.argmax(D[:, meds].min(1))))
    return meds


def cluster(xy, cap, max_dist):
    """Capacitated k-medoids. Start with ceil(n/cap) nodes and add nodes until every block is within max_dist."""
    n = len(xy)
    if n == 0:
        return [], np.zeros(0, int)
    D = _dist(xy, xy)
    meds = _init(D, xy, max(1, math.ceil(n / cap)))
    while True:
        for _ in range(40):
            asg = _assign(D, meds, cap)
            new = []
            for j in range(len(meds)):
                mem = np.where(asg == j)[0]
                new.append(int(mem[np.argmin(D[np.ix_(mem, mem)].sum(1))]))
            if new == meds:
                break
            meds = new
        asg = _assign(D, meds, cap)
        dd = D[np.arange(n), [meds[j] for j in asg]]
        bad = np.where(dd > max_dist + 1e-9)[0]
        if len(bad) == 0 or len(meds) >= n:
            return meds, asg
        meds = meds + [int(bad[np.argmax(dd[bad])])]


def max_coverage(xy, cap, max_dist, k):
    """Budget scenario: k nodes chosen greedily to monitor as many blocks as possible. Others stay unmonitored."""
    n = len(xy)
    D = _dist(xy, xy)
    free = np.ones(n, bool)
    meds, groups = [], []
    for _ in range(min(k, n)):
        best, best_set = -1, []
        for c in range(n):
            if not free[c]:
                continue
            cand = np.where(free & (D[c] <= max_dist + 1e-9))[0]
            cand = cand[np.argsort(D[c][cand])][:cap]
            if len(cand) > len(best_set):
                best, best_set = c, list(cand)
        if best < 0:
            break
        meds.append(best)
        groups.append(best_set)
        free[best_set] = False
    asg = -np.ones(n, int)
    for j, g in enumerate(groups):
        asg[g] = j
    return meds, asg


def place_gateways(nodes_xy, cands_xy, R, redundant=False, count=None):
    """Greedy set cover (multi-cover in redundant mode). Candidates are block centres."""
    if len(nodes_xy) == 0:
        return []
    cover = _dist(cands_xy, nodes_xy) <= R
    need = np.full(len(nodes_xy), 2 if redundant else 1)
    chosen = []
    while count is None or len(chosen) < count:
        gain = (cover & (need > 0)[None, :]).sum(1).astype(float)
        gain[chosen] = -1
        if gain.max() <= 0:
            break
        top = np.where(gain == gain.max())[0]
        if len(top) > 1:                  # tie: the candidate closest to the nodes it would serve
            dm = [_dist(cands_xy[[t]], nodes_xy[cover[t] & (need > 0)]).mean() for t in top]
            best = int(top[int(np.argmin(dm))])
        else:
            best = int(top[0])
        chosen.append(best)
        need = need - cover[best]
        if (need <= 0).all():
            break
    return chosen


def plan(cfg, scenario="optimised", k_nodes=None):
    """Full plan. scenario: dense (one node per block), optimised (the algorithm), budget (k_nodes, max coverage)."""
    g, rc, xy, obs = blocks(cfg)
    if len(rc) > MAX_BLOCKS_TOTAL:
        raise ValueError("too many blocks (max %d)" % MAX_BLOCKS_TOTAL)
    nd, R = cfg["node"], lora_range(cfg)
    cap, maxd = int(nd["max_blocks"]), float(nd["max_dist_m"])
    gw = cfg["gateways"]
    n = len(rc)
    if n == 0:
        meds, asg = [], np.zeros(0, int)
    elif scenario == "dense":
        meds, asg = list(range(n)), np.arange(n)
    elif scenario == "budget":
        meds, asg = max_coverage(xy, cap, maxd, int(k_nodes or 0))
    else:
        meds, asg = cluster(xy, cap, maxd)
    nodes = []
    for j, m in enumerate(meds):
        mem = [rc[i] for i in np.where(asg == j)[0]]
        nodes.append(dict(id="N%02d" % (j + 1), x=float(xy[m][0]), y=float(xy[m][1]), r=rc[m][0], c=rc[m][1], blocks=[list(b) for b in mem]))
    nxy = np.array([[a["x"], a["y"]] for a in nodes]) if nodes else np.zeros((0, 2))
    redundant = bool(gw.get("redundant"))
    count = int(gw["count"]) if gw.get("mode") == "fixed" else None
    gidx = place_gateways(nxy, xy, R, redundant, count) if nodes else []
    gateways = [dict(id="G%d" % (i + 1), x=float(xy[q][0]), y=float(xy[q][1]), r=rc[q][0], c=rc[q][1]) for i, q in enumerate(gidx)]
    links, dead, redundant_ok = [], [], 0
    for a in nodes:
        ds = sorted((math.hypot(a["x"] - G["x"], a["y"] - G["y"]), G["id"]) for G in gateways)
        inrange = [(d, gid) for d, gid in ds if d <= R]
        for rank, (d, gid) in enumerate(inrange):
            links.append(dict(node=a["id"], gateway=gid, distance_m=round(d, 1), ratio=round(d / R, 3), margin_pct=round((1 - d / R) * 100, 1), primary=rank == 0))
        if not inrange:
            a["dead"] = True
            dead.append(dict(node=a["id"], nearest_gateway=ds[0][1] if ds else None, distance_m=round(ds[0][0], 1) if ds else None))
            if ds:
                links.append(dict(node=a["id"], gateway=ds[0][1], distance_m=round(ds[0][0], 1), ratio=round(ds[0][0] / R, 3),
                                  margin_pct=round((1 - ds[0][0] / R) * 100, 1), primary=True, beyond=True))
        else:
            a["dead"] = False
            redundant_ok += len(inrange) >= 2
    dead_ids = {d["node"] for d in dead}
    monitored = sum(len(a["blocks"]) for a in nodes)
    connected = sum(len(a["blocks"]) for a in nodes if a["id"] not in dead_ids)
    assigned = {tuple(b) for a in nodes for b in a["blocks"]}
    unassigned = [list(b) for b in rc if tuple(b) not in assigned]
    dead_blocks = [b for a in nodes if a["id"] in dead_ids for b in a["blocks"]]
    far = [math.hypot(a["x"] - (b[1] + 0.5) * g["bw"], a["y"] - (b[0] + 0.5) * g["bh"]) for a in nodes for b in a["blocks"]]
    return dict(scenario=scenario, geometry=g, lora_range_m=R, usable_blocks=n, obstacles=[list(o) for o in sorted(obs)],
                nodes=nodes, gateways=gateways, links=links, dead_zones=dead, dead_blocks=dead_blocks,
                unassigned_blocks=unassigned, uncovered_blocks=unassigned + dead_blocks,
                coverage_pct=round(100.0 * connected / n, 1) if n else 0.0, monitored_pct=round(100.0 * monitored / n, 1) if n else 0.0,
                redundancy_pct=round(100.0 * redundant_ok / len(nodes), 1) if nodes else 0.0, max_dist_used_m=round(max(far or [0]), 1))


def cost(cfg, p):
    """Illustrative cost of a plan (replace the unit prices with real quotes)."""
    pr = cfg["prices_inr"]
    n_n, n_g = len(p["nodes"]), len(p["gateways"])
    per_node = pr["node"] + pr["sensors"] + pr["enclosure"] + pr["installation"] + pr["power"]
    per_gw = pr["gateway"] + pr["gateway_installation"] + pr["gateway_power"]
    capex = n_n * per_node + n_g * per_gw
    yearly = n_n * pr["maintenance_node_year"] + n_g * pr["maintenance_gateway_year"]
    acres = p["geometry"]["acres"]
    monitored = sum(len(a["blocks"]) for a in p["nodes"] if not a.get("dead"))
    return dict(nodes=n_n, gateways=n_g, per_node=per_node, per_gateway=per_gw, capex=capex, yearly=yearly, five_year=capex + 5 * yearly,
                per_acre=round(capex / acres) if acres else 0, per_block=round(capex / monitored) if monitored else None)


def scenarios(cfg):
    """A dense, B optimised, C budget-limited. Returns plans, costs and a trade-off summary (i18n key + params)."""
    A, B = plan(cfg, "dense"), plan(cfg, "optimised")
    cA, cB = cost(cfg, A), cost(cfg, B)
    budget, assumed = cfg.get("budget_inr"), False
    if not budget:
        budget, assumed = round(cB["capex"] * 0.7, -2), True
    if cB["capex"] <= budget:
        C = plan(cfg, "optimised")
    else:                                  # fewest nodes that fit the budget: largest k whose cost is within budget
        lo, hi, best = 0, len(B["nodes"]), None
        while lo <= hi:
            mid = (lo + hi) // 2
            pc = plan(cfg, "budget", mid) if mid > 0 else None
            if pc and cost(cfg, pc)["capex"] <= budget:
                best, lo = pc, mid + 1
            else:
                hi = mid - 1
        C = best or plan(cfg, "budget", 0)
    cC = cost(cfg, C)
    out = {k: dict(plan=p, cost=c) for k, p, c in (("A", A, cA), ("B", B, cB), ("C", C, cC))}
    saved = round(100 * (1 - cB["capex"] / cA["capex"])) if cA["capex"] else 0
    out["summary"] = dict(key="plan.trade", p=dict(saved=saved, nodesA=cA["nodes"], nodesB=cB["nodes"], covB=B["coverage_pct"],
                                                   lost=round(B["coverage_pct"] - C["coverage_pct"], 1), covC=C["coverage_pct"], budget=int(budget)))
    out["budget_inr"] = budget
    out["budget_assumed"] = assumed
    return out
