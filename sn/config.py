"""Shared constants: the panel 'digital twin', fault classes, money assumptions.

The PANEL values are a starting twin for a 6 V / 250 mA mini panel under the lamp.
Tonight they get replaced by values fitted to our own measured healthy curves
(see sn/twin.py).
"""

# ---- Panel digital twin (single-diode / De Soto reference parameters) ----
PANEL = dict(
    I_L_ref=0.25,      # A, photocurrent at full lamp light (g = 1.0)
    I_o_ref=4.0e-9,    # A, diode saturation current
    R_s=0.5,           # ohm, internal series resistance
    R_sh_ref=300.0,    # ohm, shunt resistance
    a_ref=0.40,        # V, n * Ns * Vth at 25 C  (gives Voc ~ 7.2 V per panel)
    alpha_sc=0.25 * 0.0005,  # A/C, Isc temperature coefficient (+0.05 %/C)
)
N_PANELS = 2               # Panel A + Panel B in series
REF_CELL_ISC_A = 0.080     # A, reference cell short-circuit current at g = 1.0
REF_ALPHA = 0.0005         # 1/C, reference cell Isc temperature coefficient
BYPASS_DROP_V = 0.35       # V, Schottky bypass diode forward drop when conducting
WIRING_R = 0.25            # ohm, shunt + MOSFET + wiring: the sweep cannot go below I*R

# ---- Fault classes ----
CLASSES = ["healthy", "soiling", "partial_shade", "series_resistance", "bypass_short"]
NICE = {
    "healthy": "Healthy",
    "soiling": "Soiling / dust",
    "partial_shade": "Partial shading",
    "series_resistance": "Loose connector (high series resistance)",
    "bypass_short": "Bypass diode failed (short)",
}
ACTION = {
    "healthy": "No action needed.",
    "soiling": "Clean the panels (water + soft brush, early morning or evening).",
    "partial_shade": "Find the shadow (tree, water tank, new structure) and trim or relocate it.",
    "series_resistance": "Technician: inspect MC4 connectors and cable joints for looseness or corrosion.",
    "bypass_short": "Technician: the bypass diode in panel B's junction box has failed short; replace it.",
}

# ---- Money assumptions (shown and editable in the dashboard) ----
DEFAULT_KWP = 3.0          # installation size the % loss is scaled to
DEFAULT_YIELD = 3.8        # kWh per kWp per day (~1,400 kWh/kWp/year)
DEFAULT_TARIFF = 6.0       # Rs per kWh

# ---- Decision thresholds ----
MIN_CONFIDENCE = 0.60      # below this the diagnosis is "uncertain"
