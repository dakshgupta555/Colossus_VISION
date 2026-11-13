"""Current calibration (data/calibration.json).

raw_current_ma is never modified. calibrated = raw * gain + offset.
Defaults: gain 1.0, offset 0, status PROVISIONAL (nobody has checked it against a meter).
Verification: the owner reads a multimeter in series at the same moment as the INA219 average;
gain = meter / INA219 average, status VERIFIED, with the date. Nothing ever adjusts itself.
"""
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.environ.get("SN_CALIB") or os.path.join(ROOT, "data", "calibration.json")
DEFAULT = dict(gain=1.0, offset=0.0, status="PROVISIONAL", verified_at=None, meter_ma=None, ina_avg_ma=None)
MIN_AVG_MA = 0.3          # below this the INA219 average is mostly noise (0.1 mA steps)
GAIN_RANGE = (0.2, 5.0)   # a gain outside this means a wiring or reading mistake, not a calibration


def load():
    try:
        return {**DEFAULT, **json.load(open(PATH))}
    except (OSError, ValueError):
        return dict(DEFAULT)


def apply(raw_ma, cal=None):
    if raw_ma is None:
        return None
    cal = cal or load()
    return raw_ma * cal["gain"] + cal["offset"]


def verify(meter_ma, ina_avg_ma):
    """Store a verified gain. Raises ValueError with an i18n key if the numbers are not usable."""
    if not meter_ma or meter_ma <= 0:
        raise ValueError("cal.err.meter")
    if ina_avg_ma is None or ina_avg_ma < MIN_AVG_MA:
        raise ValueError("cal.err.low")
    gain = meter_ma / ina_avg_ma
    if not GAIN_RANGE[0] <= gain <= GAIN_RANGE[1]:
        raise ValueError("cal.err.range")
    cal = dict(gain=round(gain, 5), offset=0.0, status="VERIFIED", verified_at=time.time(),
               meter_ma=meter_ma, ina_avg_ma=round(ina_avg_ma, 4))
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    json.dump(cal, open(PATH, "w"), indent=1)
    return cal


def reset():
    if os.path.exists(PATH):
        os.remove(PATH)
    return dict(DEFAULT)
