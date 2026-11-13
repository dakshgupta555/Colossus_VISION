"""Feature extraction. The SAME function runs on simulated and real sweeps."""
import numpy as np
from scipy.signal import find_peaks, savgol_filter

from . import config as C
from . import pv_sim

SHAPE_POINTS = np.round(np.arange(0.1, 0.91, 0.1), 2)
FEATURE_NAMES = (
    ["isc_ratio", "voc_ratio", "pmax_ratio", "ff", "ff_ratio", "rs_norm", "rs_ratio",
     "step_score", "n_peaks", "vmp_voc", "imp_isc", "g_est", "temp_c"]
    + [f"shape_{int(x * 100)}" for x in SHAPE_POINTS]
)


def curve_basics(points):
    """Isc, Voc, Pmax, FF and shape descriptors from a list of [V, mA]."""
    a = np.asarray(points, dtype=float)
    V, I = a[:, 0], a[:, 1] / 1000.0
    order = np.argsort(V)
    V, I = V[order], I[order]
    voc = float(V.max())
    # Isc: straight-line fit through the low-voltage points, extrapolated to 0 V
    low = V < 0.12 * voc
    if low.sum() < 3:
        low = np.zeros_like(V, dtype=bool)
        low[:4] = True
    b, a0 = np.polyfit(V[low], I[low], 1)
    isc = float(max(a0, I.max() * 0.5))
    P = V * I
    k = int(np.argmax(P))
    pmax, vmp, imp = float(P[k]), float(V[k]), float(I[k])
    ff = pmax / max(voc * isc, 1e-9)

    # resample on a normalised voltage grid
    vn = np.linspace(0.02, 0.98, 49)
    # make V strictly increasing for interpolation
    Vu, idx = np.unique(V, return_index=True)
    Iu = I[idx]
    In = np.interp(vn * voc, Vu, Iu) / max(isc, 1e-9)
    In_s = savgol_filter(In, 7, 2) if In.size >= 7 else In
    dIdV = -np.gradient(In_s, vn)
    win = (vn >= 0.15) & (vn <= 0.7)
    step_score = float(dIdV[win].max())
    tail = vn >= 0.9
    slope_voc = float(np.mean(dIdV[tail]))
    rs_norm = 1.0 / max(slope_voc, 1e-3)
    Pn = In_s * vn
    peaks, _ = find_peaks(Pn, prominence=0.03 * max(Pn.max(), 1e-9))
    n_peaks = int(max(len(peaks), 1))
    shape = np.interp(SHAPE_POINTS, vn, In_s)
    return dict(isc=isc, voc=voc, pmax=pmax, vmp=vmp, imp=imp, ff=ff, rs_norm=rs_norm,
                step_score=step_score, n_peaks=n_peaks, vmp_voc=vmp / voc, imp_isc=imp / isc,
                shape=shape, V=V, I=I)


def light_from_ref(ref_isc_ma, temp_c):
    """Relative light level g from the clean reference cell."""
    return ref_isc_ma / (C.REF_CELL_ISC_A * 1000.0 * (1 + C.REF_ALPHA * (temp_c - 25.0)))


_EXP_CACHE = {}


def expected_basics(g, temp_c, panel=None):
    key = (round(g, 3), round(temp_c, 1), id(panel))
    if key not in _EXP_CACHE:
        if len(_EXP_CACHE) > 5000:
            _EXP_CACHE.clear()
        pts = pv_sim.expected_curve(g, temp_c, panel=panel)
        _EXP_CACHE[key] = (curve_basics(pts), pts)
    return _EXP_CACHE[key]


def extract(payload, panel=None):
    """Return (feature dict, measured basics, expected basics, expected points)."""
    temp = float(payload["temp_c"])
    g = light_from_ref(float(payload["ref_isc_ma"]), temp)
    m = curve_basics(payload["points"])
    e, e_pts = expected_basics(g, temp, panel)
    f = dict(
        isc_ratio=m["isc"] / e["isc"], voc_ratio=m["voc"] / e["voc"],
        pmax_ratio=m["pmax"] / e["pmax"], ff=m["ff"], ff_ratio=m["ff"] / e["ff"],
        rs_norm=m["rs_norm"], rs_ratio=m["rs_norm"] / e["rs_norm"],
        step_score=m["step_score"], n_peaks=m["n_peaks"], vmp_voc=m["vmp_voc"],
        imp_isc=m["imp_isc"], g_est=g, temp_c=temp,
    )
    for x, val in zip(SHAPE_POINTS, m["shape"]):
        f[f"shape_{int(x * 100)}"] = float(val)
    return f, m, e, e_pts


def vector(f):
    return [float(f[n]) for n in FEATURE_NAMES]
