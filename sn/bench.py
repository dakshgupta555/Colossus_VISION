"""Real-bench helpers (one panel, INA219, jumper on = short circuit, jumper off = open circuit).

Two numbers are measured: Isc (short circuit) and Voc (open circuit). Everything else here is a
PHYSICS MODEL through those two points, never a measurement:
  - findings(): rule-based reading of the change against a healthy baseline
  - anchored_curve(): a pvlib single-diode curve forced through the measured Isc and Voc
  - whatif(): what dust, partial shade and a loose joint would look like at the same loss, on our twin
"""
import functools

import numpy as np
from pvlib import pvsystem

from . import config as C, pv_sim as S

ISC_DROP_PCT = 8.0       # Isc down by more than this = less light reaching the panel
VOC_SAME_PCT = 5.0       # ... when Voc stays within this
VOC_DROP_PCT = 15.0      # Voc down by more than this = voltage loss
RSH = 1e7                # ohm: effectively no shunt leakage at these tiny currents


def pct_change(now, base):
    return None if not base else (now / base - 1.0) * 100.0


def finding(isc_pct, voc_pct):
    """i18n key of the rule-based finding, from % change of Isc and Voc against the baseline."""
    if isc_pct < -ISC_DROP_PCT and abs(voc_pct) <= VOC_SAME_PCT:
        return "finding.light"
    if voc_pct < -VOC_DROP_PCT:
        return "finding.voltage"
    if abs(isc_pct) > ISC_DROP_PCT:          # moved, but not in a way the two rules above describe
        return "finding.unclear"
    return "finding.ok"


def approx_loss(isc, voc, isc_b, voc_b):
    """Approximate power loss fraction, 1 - (Isc*Voc)/(Isc_base*Voc_base). An approximation, never below 0."""
    return max(0.0, 1.0 - (isc * voc) / (isc_b * voc_b))


@functools.lru_cache(maxsize=1)
def _knee_ratio():
    """a / Voc of the twin's panel: how sharp the knee is. Used because two points cannot fix it."""
    p = S.module_params(1.0, 25.0)
    i, v = S.module_iv(p)
    return p[4] / float(np.interp(0.0, i, v))


def anchored_curve(isc_ma, voc_v, n=80):
    """pvlib single-diode curve through (0 V, Isc) and (Voc, 0 mA). Returns dict with points and Pmax."""
    il = isc_ma / 1000.0
    a = _knee_ratio() * voc_v
    i0 = (il - voc_v / RSH) / np.expm1(voc_v / a)
    v = np.linspace(0.0, voc_v, n)
    i = np.asarray(pvsystem.i_from_v(v, il, i0, 0.0, RSH, a, method="lambertw"), dtype=float) * 1000.0
    i = np.clip(i, 0.0, None)
    i[0], i[-1] = isc_ma, 0.0
    k = int(np.argmax(v * i))
    return dict(isc_ma=float(isc_ma), voc_v=float(voc_v),
                points=[[round(float(x), 4), round(float(y), 4)] for x, y in zip(v, i)],
                pmax_mw=float(v[k] * i[k]), vmp_v=float(v[k]), imp_ma=float(i[k]))


def _pmax(V, I):
    return float(np.max(V * I))


def _bisect(loss_of, target, lo, hi, iters=22):
    """Smallest-effort solve of loss_of(x) = target for increasing loss_of. Returns (x, reached)."""
    if loss_of(hi) < target:
        return hi, False
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if loss_of(mid) < target else (lo, mid)
    return 0.5 * (lo + hi), True


@functools.lru_cache(maxsize=64)
def whatif(loss_pct):
    """Normalised curves (x = V/Voc, y = I/Isc of the healthy twin) for dust, shade and a loose joint,
    each tuned to the same power loss. Physics model of our two-panel twin, scaled; not measured."""
    target = loss_pct / 100.0
    g, t = 0.9, 38.0
    base = [S.module_params(g, t) for _ in range(C.N_PANELS)]
    V0, I0 = S.string_iv(base)
    P0, vmax, imax = _pmax(V0, I0), float(V0.max()), float(I0.max())

    def loss_of(build):
        V, I = S.string_iv(build)
        return 1.0 - _pmax(V, I) / P0

    def dust(s):
        return [S.module_params(g, t, 1 - s) for _ in range(C.N_PANELS)]

    def shade(f):
        return [S.module_params(g, t), S.module_params(g, t, 1 - f)]

    s, _ = _bisect(lambda x: loss_of(dust(x)), target, 0.0, 0.98)
    f, reach = _bisect(lambda x: loss_of(shade(x)), target, 0.0, 0.99)
    r, _ = _bisect(lambda x: 1.0 - _pmax(*S.string_iv(base, extra_r=x)) / P0, target, 0.0, 400.0)

    def norm(V, I, m=70):
        idx = np.linspace(0, len(V) - 1, m).astype(int)
        x, y = V[idx] / vmax, I[idx] / imax
        o = np.argsort(x)
        return [[round(float(a), 4), round(float(b), 4)] for a, b in zip(x[o], y[o])]

    return dict(loss_pct=loss_pct, healthy=norm(V0, I0), dust=norm(*S.string_iv(dust(s))),
                shade=norm(*S.string_iv(shade(f))),
                joint=norm(*S.string_iv(base, extra_r=r)), shade_reached=bool(reach))
