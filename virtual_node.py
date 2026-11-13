"""Virtual node: posts SIMULATED sweeps to the API exactly like the ESP32 will.

Examples:
  python virtual_node.py                       # cycles through all faults every 4 s
  python virtual_node.py --fault partial_shade --severity 0.6
  python virtual_node.py --fault soiling --severity 0.3 --count 1
Faults: healthy, soiling, partial_shade, series_resistance, bypass_short, cycle
Every sweep is tagged source="sim" so the dashboard marks it SIMULATED.
"""
import argparse
import itertools
import time

import numpy as np
import requests

from sn import config as C, pv_sim as S

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8000")
ap.add_argument("--fault", default="cycle", choices=C.CLASSES + ["cycle"])
ap.add_argument("--severity", type=float, default=None,
                help="soiling/shade fraction 0-1, or extra ohms for series_resistance")
ap.add_argument("--light", type=float, default=0.9, help="relative light 0.2-1.1")
ap.add_argument("--temp", type=float, default=38.0)
ap.add_argument("--interval", type=float, default=4.0)
ap.add_argument("--count", type=int, default=0, help="0 = forever")
ap.add_argument("--node", default="SN-SIM-01")
a = ap.parse_args()

rng = np.random.default_rng()
faults = itertools.cycle(C.CLASSES) if a.fault == "cycle" else itertools.repeat(a.fault)
for seq in itertools.count(1):
    fault = next(faults)
    s = S.make_scenario(fault, rng)
    s["g"], s["temp_c"] = a.light, a.temp
    if a.severity is not None:
        if fault == "soiling":
            s["soiling"] = a.severity
        elif fault == "partial_shade":
            s["shade"], s["shade_panel"] = a.severity, 1
        elif fault == "series_resistance":
            s["extra_r"] = a.severity
    p = S.simulate(s, rng)
    p.update(node_id=a.node, seq=seq, source="sim", fw="virtual-0.1")
    try:
        r = requests.post(a.url + "/sweeps", json=p, timeout=10).json()
        print(f"#{seq} injected={fault:18s} -> {r['headline']} ({r['confidence']:.0%}), "
              f"loss {r['loss_pct']}%, LED {r['led']}, alert {r['alert']}")
    except Exception as exc:
        print("API not reachable:", exc)
    if a.count and seq >= a.count:
        break
    time.sleep(a.interval)
