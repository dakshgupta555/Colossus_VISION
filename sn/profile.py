"""Panel profile (data/panel_profile.json) and its pvlib twin.

Presets:
  bench    "Our bench panel (from our measurements)": Voc/Isc come from the measured baseline; the rated
           numbers are the NOMINAL 6 V / 250 mA label of the mini panel from sn/config.py (unverified).
  mono540  "Example 540 Wp mono-PERC module (typical datasheet values, not a specific brand)".
A profile with Vmp/Imp gets a single-diode twin from pvlib.ivtools.sdm.fit_desoto; the bench preset uses the
anchored curve through the measured Isc/Voc (sn/bench.py) because its Vmp/Imp are not measured.
"""
import functools
import json
import os

import numpy as np
from pvlib import pvsystem
from pvlib.ivtools import sdm

from . import config as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.environ.get("SN_PROFILE") or os.path.join(ROOT, "data", "panel_profile.json")
FIELDS = ["name", "manufacturer", "model", "pmax_w", "voc_v", "isc_a", "vmp_v", "imp_a", "alpha_sc_pct", "beta_voc_pct",
          "gamma_pmax_pct", "cells_series", "panels_series", "strings_parallel", "kwp", "cells_x", "cells_y", "note"]

PRESETS = {
    "bench": dict(name="Our bench panel (from our measurements)", manufacturer="", model="mini panel",
                  pmax_w=None, voc_v=None, isc_a=C.PANEL["I_L_ref"], vmp_v=None, imp_a=None,
                  alpha_sc_pct=0.05, beta_voc_pct=-0.3, gamma_pmax_pct=None, cells_series=None,
                  panels_series=1, strings_parallel=1, kwp=C.DEFAULT_KWP, cells_x=6, cells_y=6,
                  note="Voc and Isc come from your baseline. The rated Isc (250 mA) is the nominal label value, unverified. "
                       "Money is scaled to the kWp below because the bench panel itself delivers milliwatts."),
    "mono540": dict(name="Example 540 Wp mono-PERC module (typical datasheet values, not a specific brand)",
                    manufacturer="", model="540 Wp mono-PERC", pmax_w=540.0, voc_v=49.5, isc_a=13.85, vmp_v=41.6,
                    imp_a=12.98, alpha_sc_pct=0.048, beta_voc_pct=-0.27, gamma_pmax_pct=-0.35, cells_series=72,
                    panels_series=1, strings_parallel=1, kwp=0.54, cells_x=6, cells_y=12,
                    note="Typical values for the class, not a specific product."),
}


def _store():
    try:
        return json.load(open(PATH))
    except (OSError, ValueError):
        return {}


def all_profiles():
    st = _store()
    out = {}
    for pid, base in PRESETS.items():
        out[pid] = {**base, **{k: v for k, v in st.get("profiles", {}).get(pid, {}).items() if k in FIELDS}}
    for pid, p in st.get("profiles", {}).items():
        if pid not in PRESETS:
            out[pid] = {k: p.get(k) for k in FIELDS}
    return out


def active_id():
    st = _store()
    pid = st.get("active", "bench")
    return pid if pid in all_profiles() else "bench"


def get(pid=None):
    pid = pid or active_id()
    return {**all_profiles()[pid], "id": pid}


def save(pid, fields):
    """Save fields for a profile id (a preset id overrides its defaults; any other id is a custom profile)."""
    st = _store()
    st.setdefault("profiles", {})
    clean = {k: fields.get(k) for k in FIELDS if k in fields}
    for k in ("pmax_w", "voc_v", "isc_a", "vmp_v", "imp_a", "alpha_sc_pct", "beta_voc_pct", "gamma_pmax_pct", "kwp"):
        if k in clean and clean[k] in ("", None):
            clean[k] = None
        elif k in clean:
            clean[k] = float(clean[k])
    for k in ("cells_series", "panels_series", "strings_parallel", "cells_x", "cells_y"):
        if k in clean and clean[k] not in ("", None):
            clean[k] = int(clean[k])
        elif k in clean:
            clean[k] = None
    st["profiles"][pid] = {**st["profiles"].get(pid, {}), **clean}
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    json.dump(st, open(PATH, "w"), indent=1)
    return get(pid)


def set_active(pid):
    if pid not in all_profiles():
        raise KeyError(pid)
    st = _store()
    st["active"] = pid
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    json.dump(st, open(PATH, "w"), indent=1)


def has_datasheet(p):
    return all(p.get(k) for k in ("voc_v", "isc_a", "vmp_v", "imp_a", "cells_series"))


def _guess(voc, isc, vmp, imp, cells, k):
    a0 = k * 0.025693 * cells
    return dict(a_0=a0, IL_0=isc, Io_0=isc / np.expm1(voc / a0), Rs_0=max(0.05, (voc - vmp) / imp * 0.35), Rsh_0=voc / isc * 30)


@functools.lru_cache(maxsize=16)
def _fit(voc, isc, vmp, imp, alpha_pct, beta_pct, cells):
    """fit_desoto is fragile on its default start, so try a few physically sensible starting points."""
    alpha = isc * (alpha_pct or 0.05) / 100.0           # A/degC
    beta = voc * (beta_pct or -0.3) / 100.0             # V/degC
    last = None
    for k, kw in ((1.2, {}), (1.0, {}), (1.3, {}), (1.2, dict(method="lm")), (0.9, {})):
        try:
            params, _ = sdm.fit_desoto(v_mp=vmp, i_mp=imp, v_oc=voc, i_sc=isc, alpha_sc=alpha, beta_voc=beta,
                                       cells_in_series=cells, init_guess=_guess(voc, isc, vmp, imp, cells, k), root_kwargs=kw)
            return params
        except Exception as exc:
            last = exc
    raise last


def fit(p):
    """pvlib De Soto parameters from the datasheet numbers. Raises ValueError('profile.err.fit') if they do not fit."""
    if not has_datasheet(p):
        raise ValueError("profile.err.incomplete")
    try:
        return _fit(p["voc_v"], p["isc_a"], p["vmp_v"], p["imp_a"], p.get("alpha_sc_pct"), p.get("beta_voc_pct"), p["cells_series"])
    except Exception as exc:
        raise ValueError("profile.err.fit") from exc


def expected(p, g_rel, temp_c=25.0):
    """Datasheet twin: Isc, Voc, Pmax of ONE module at relative light g_rel (1.0 = 1000 W/m2) and cell temperature."""
    d = fit(p)
    IL, I0, Rs, Rsh, a = pvsystem.calcparams_desoto(max(g_rel, 0.001) * 1000.0, temp_c, d["alpha_sc"], d["a_ref"],
                                                    d["I_L_ref"], d["I_o_ref"], d["R_sh_ref"], d["R_s"], EgRef=d["EgRef"], dEgdT=d["dEgdT"])
    s = pvsystem.singlediode(IL, I0, Rs, Rsh, a, method="lambertw")
    return dict(isc_a=float(s["i_sc"]), voc_v=float(s["v_oc"]), pmax_w=float(s["p_mp"]), vmp_v=float(s["v_mp"]), imp_a=float(s["i_mp"]))


def equivalent_light_w_m2(isc_meas_ma, p):
    """Approximate: measured Isc / rated Isc x 1000 W/m2. Temperature and spectrum are unknown."""
    if not p.get("isc_a") or isc_meas_ma is None:
        return None
    return max(0.0, isc_meas_ma / 1000.0 / p["isc_a"] * 1000.0)
