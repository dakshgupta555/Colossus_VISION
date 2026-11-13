"""LDR light sensors (data/sensors.json): roles, normalisation to each sensor's own baseline, shaded regions.

LDRs are non-linear and uncalibrated, so the only number ever shown is "relative light, % of that
sensor's own baseline". A region is flagged as shaded ONLY when its LDR dropped more than 25% below
its baseline while the reference LDR did not.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.environ.get("SN_SENSORS") or os.path.join(ROOT, "data", "sensors.json")
REGION_DROP = 0.25        # region LDR below 75% of its baseline = shaded
REF_STEADY = 0.90         # reference LDR at or above 90% of its baseline = "did not drop"
# Position of each region on the panel face, x right, y up, both in [-1, 1].
REGION_POS = {"top-left": (-0.7, 0.7), "top-right": (0.7, 0.7), "bottom-left": (-0.7, -0.7),
              "bottom-right": (0.7, -0.7), "left": (-0.7, 0.0), "right": (0.7, 0.0)}
REFERENCE_POS = (1.22, 0.78)          # beside the panel


def load():
    try:
        cfg = json.load(open(PATH))
    except (OSError, ValueError):
        cfg = {}
    return [s for s in cfg.get("sensors", []) if s.get("id")]


def rel_pct(raw, base, invert=False):
    """Relative light in % of this sensor's own baseline (None if either value is missing)."""
    if raw is None or base is None or raw <= 0 or base <= 0:
        return None
    return (base / raw if invert else raw / base) * 100.0


def normalise(ldr_raw, ldr_base, cfg=None):
    """One row per configured sensor, aligned by list position. Missing values stay None (never invented)."""
    cfg = cfg if cfg is not None else load()
    rows = []
    for i, s in enumerate(cfg):
        raw = ldr_raw[i] if ldr_raw and i < len(ldr_raw) else None
        base = ldr_base[i] if ldr_base and i < len(ldr_base) else None
        pos = REFERENCE_POS if s.get("role") == "reference" else REGION_POS.get(s.get("region"))
        rows.append(dict(id=s["id"], name=s.get("name", s["id"]), pin=s.get("pin"), role=s.get("role", "region"),
                         region=s.get("region"), raw=raw, base=base, pos=list(pos) if pos else None,
                         rel_pct=rel_pct(raw, base, s.get("invert", False))))
    return rows


def analyse(rows):
    """Light-sensor evidence: reference ratio, shaded regions, uniform cover vs partial shade."""
    ref = next((r for r in rows if r["role"] == "reference" and r["rel_pct"] is not None), None)
    regs = [r for r in rows if r["role"] == "region" and r["rel_pct"] is not None]
    out = dict(ref_ratio=ref["rel_pct"] / 100.0 if ref else None, shaded=[], pattern=None, have_ref=bool(ref),
               have_regions=bool(regs))
    if not regs:
        return out
    ref_steady = ref is None or ref["rel_pct"] / 100.0 >= REF_STEADY
    low = [r for r in regs if r["rel_pct"] / 100.0 < 1.0 - REGION_DROP]
    if ref_steady:
        out["shaded"] = [r["region"] for r in low]
    if low and ref_steady:
        out["pattern"] = "uniform" if len(low) == len(regs) and len(regs) > 1 else "partial"
    return out


def cells_in_region(region, nx, ny):
    """Cell (col,row) indices covered by a region (row 0 = top)."""
    cells = []
    for r in range(ny):
        for c in range(nx):
            top, left = r < ny / 2, c < nx / 2
            hit = {"top-left": top and left, "top-right": top and not left, "bottom-left": (not top) and left,
                   "bottom-right": (not top) and (not left), "left": left, "right": not left}.get(region, False)
            if hit:
                cells.append([c, r])
    return cells
