"""Physics simulator: two mini panels in series with bypass diodes, plus faults.

Every curve produced here is SIMULATED. Payloads carry source="sim" so the
dashboard can label them.
"""
import numpy as np
from pvlib import pvsystem, singlediode

from . import config as C


def module_params(g, temp_c, iph_scale=1.0, rs_scale=1.0, panel=None):
    """Single-diode parameters of one panel at relative light g and cell temperature."""
    p = panel or C.PANEL
    g = max(float(g), 0.02)
    IL, I0, Rs, Rsh, nNsVth = pvsystem.calcparams_desoto(
        effective_irradiance=g * 1000.0, temp_cell=float(temp_c),
        alpha_sc=p["alpha_sc"], a_ref=p["a_ref"], I_L_ref=p["I_L_ref"],
        I_o_ref=p["I_o_ref"], R_sh_ref=p["R_sh_ref"], R_s=p["R_s"],
    )
    return (float(IL) * iph_scale, float(I0), float(Rs) * rs_scale, float(Rsh), float(nNsVth))


def module_iv(params, n=1500):
    """Full I-V of one panel, including a little reverse bias. Returns (I asc, V)."""
    IL, I0, Rs, Rsh, nNsVth = params
    vd_max = nNsVth * np.log(IL / I0 + 1.0) + 0.05
    vd = np.linspace(-2.5, vd_max, n)
    i, v, _ = singlediode.bishop88(vd, IL, I0, Rs, Rsh, nNsVth)
    order = np.argsort(i)
    return np.asarray(i)[order], np.asarray(v)[order]


def string_iv(mods, extra_r=0.0, shorted=None, n=1200, return_panels=False):
    """Series string with a bypass diode across each panel.

    mods: list of single-diode parameter tuples, one per panel.
    extra_r: added series resistance (loose connector), ohm.
    shorted: index of a panel whose bypass diode has failed short (its voltage is 0).
    Returns (V, I) from Voc down to the lowest reachable voltage (V >= 0).
    """
    i_max = max(m[0] for m in mods) * 1.03 + 0.005
    I = np.linspace(0.0, i_max, n)
    V = np.zeros_like(I)
    parts = []
    for k, m in enumerate(mods):
        if shorted is not None and k == shorted:
            parts.append(np.zeros_like(I))  # shorted bypass diode: this panel contributes ~0 V
            continue
        ik, vk = module_iv(m)
        Vk = np.interp(I, ik, vk, left=vk[0], right=-C.BYPASS_DROP_V)
        Vk = np.maximum(Vk, -C.BYPASS_DROP_V)  # bypass diode clamps reverse voltage
        parts.append(Vk)
        V += Vk
    V -= I * extra_r
    keep = V >= 0
    if return_panels:
        return V[keep], I[keep], [q[keep] for q in parts]
    return V[keep], I[keep]


def sweep_points(V, I, n_points=80, rng=None, noise=True):
    """Sample the curve like the firmware's held-point sweep, then add INA219-like noise.

    Returns a list of [volts, milliamps], ordered from short circuit to open circuit,
    with the gate-off Voc point last.
    """
    rng = rng or np.random.default_rng()
    voc = V.max()
    isc = I.max()
    reach = V >= I * C.WIRING_R          # MOSFET + shunt + wires: can't reach exactly 0 V
    Vr, Ir = V[reach], I[reach]
    order = np.argsort(Vr)
    Vr, Ir = Vr[order], Ir[order]
    x, y = Vr / voc, Ir / max(isc, 1e-9)
    s = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])
    t = np.linspace(0.0, s[-1], n_points)
    vs = np.interp(t, s, Vr)
    is_ = np.interp(t, s, Ir)
    vs = np.append(vs, voc)
    is_ = np.append(is_, 0.0)
    ma = is_ * 1000.0
    if noise:
        vs = vs + rng.normal(0, 0.004, vs.size)
        ma = ma * (1 + rng.normal(0, 0.003, ma.size)) + rng.normal(0, 0.25, ma.size) + rng.normal(0, 0.3)
        vs = np.round(vs / 0.004) * 0.004
        ma = np.round(ma, 1)
    ma = np.clip(ma, 0.0, None)
    vs = np.clip(vs, 0.0, None)
    return [[round(float(a), 4), round(float(b), 2)] for a, b in zip(vs, ma)]


def ref_isc_ma(g, temp_c):
    return C.REF_CELL_ISC_A * g * (1 + C.REF_ALPHA * (temp_c - 25.0)) * 1000.0


def make_scenario(fault, rng, hard=False):
    """Random but labelled operating condition for one simulated sweep."""
    s = dict(fault=fault, shade=0.0, shade_panel=1, soiling=0.0, extra_r=0.0, shorted=None)
    s["g"] = rng.uniform(0.15, 0.35) if hard else rng.uniform(0.35, 1.1)
    s["temp_c"] = rng.uniform(55, 68) if hard else rng.uniform(25, 55)
    s["iph_scale"] = [1 + rng.normal(0, 0.015) for _ in range(C.N_PANELS)]
    s["rs_scale"] = [rng.uniform(0.9, 1.1) for _ in range(C.N_PANELS)]
    if fault == "soiling":
        s["soiling"] = rng.uniform(0.06, 0.12) if hard else rng.uniform(0.12, 0.5)
    elif fault == "partial_shade":
        s["shade"] = rng.uniform(0.12, 0.25) if hard else rng.uniform(0.25, 0.95)
        s["shade_panel"] = int(rng.integers(0, C.N_PANELS))
    elif fault == "series_resistance":
        s["extra_r"] = rng.uniform(2.0, 4.0) if hard else rng.uniform(4.0, 20.0)
    elif fault == "bypass_short":
        s["shorted"] = int(rng.integers(0, C.N_PANELS))
    return s


def simulate(s, rng=None, noise=True, n_points=80):
    """Turn a scenario into a sweep payload exactly like the ESP32 will send."""
    rng = rng or np.random.default_rng()
    mods = []
    for k in range(C.N_PANELS):
        scale = s.get("iph_scale", [1.0] * C.N_PANELS)[k] * (1 - s.get("soiling", 0.0))
        if k == s.get("shade_panel", 1):
            scale *= (1 - s.get("shade", 0.0))
        mods.append(module_params(s["g"], s["temp_c"], scale, s.get("rs_scale", [1.0] * C.N_PANELS)[k]))
    V, I, parts = string_iv(mods, extra_r=s.get("extra_r", 0.0), shorted=s.get("shorted"), return_panels=True)
    pts = sweep_points(V, I, n_points=n_points, rng=rng, noise=noise)
    temp = s["temp_c"] + (rng.normal(0, 0.3) if noise else 0.0)
    ref = ref_isc_ma(s["g"], s["temp_c"]) * ((1 + rng.normal(0, 0.004)) if noise else 1.0)
    # Mid-point sensor: voltage across panel A (ground side) at every sweep point, as an ESP32 ADC
    # on a divider would read it: coarse, noisy, and unable to go below 0 V. Uses its own noise
    # generator so the main random stream (and therefore the training set) is unchanged.
    i_pts = np.array([q[1] for q in pts]) / 1000.0
    mid = np.interp(i_pts, I, parts[0])
    if noise:
        mid = mid + np.random.default_rng(int(pts[-1][0] * 1000) + 17).normal(0, 0.03, mid.size)
    mid = np.clip(np.round(mid, 2), 0.0, None)
    return dict(points=pts, temp_c=round(float(temp), 2), ref_isc_ma=round(float(ref), 2),
                mid_v=[float(q) for q in mid])


def expected_curve(g, temp_c, panel=None, n_points=120):
    """Healthy curve the digital twin expects at this light and temperature (no noise)."""
    mods = [module_params(g, temp_c, panel=panel) for _ in range(C.N_PANELS)]
    V, I = string_iv(mods)
    return sweep_points(V, I, n_points=n_points, noise=False)
