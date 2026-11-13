# SuryaNidaan: AI diagnostics for rooftop solar (COLOSSUS 3.0)

Reads a solar panel's I-V curve, names the fault (dust, shade, loose connector, failed bypass diode),
explains why, estimates the money lost, and alerts the owner.

## Run it (3 terminals, all inside the project folder with the venv active)

```
pip install -r requirements.txt
python train.py                                   # ~1 min: trains the model, prints rules-vs-AI benchmark
uvicorn api:app --host 0.0.0.0 --port 8000        # terminal 1: backend + console at http://localhost:8000
python virtual_node.py                            # terminal 2 (optional): simulated node posting sweeps
python serial_bridge.py                           # terminal 2 (optional): LIVE ESP32 readings over USB (read-only)
streamlit run dashboard.py                        # backup dashboard only
```

Telegram alerts (optional): message @BotFather, `/newbot`, copy the token, send "hi" to your bot, then
`python setup_telegram.py <TOKEN>`.

## Project layout

| File | What it does |
| --- | --- |
| `sn/config.py` | Panel digital twin parameters, fault classes, Rs assumptions |
| `sn/pv_sim.py` | Physics simulator: 2 panels in series with bypass diodes + fault injection + sensor noise (pvlib) |
| `sn/features.py` | Features from any curve, real or simulated (Isc, Voc, Pmax, FF, slope, step, shape) |
| `sn/rules.py` | Physics-rule baseline (also a guardrail) |
| `sn/model.py` | Simulated dataset, calibrated Random Forest, anomaly check, rules-vs-AI benchmark |
| `sn/engine.py` | Diagnosis: label, confidence, evidence, action, power loss, Rs/day |
| `sn/alerts.py` | Telegram alerts with an offline retry queue, sent in the console's language |
| `sn/i18n.py` | Translation layer: every sentence in English, Hindi and Kannada (console + Telegram) |
| `api.py` | FastAPI: `POST /sweeps`, `GET /latest`, `GET /history` + SQLite |
| `web/index.html` | Main console (served by the API at `/`): Diagnosis, Fleet, Model & validation, Scan log |
| `dashboard.py` | Backup Streamlit dashboard |
| `virtual_node.py` | Posts SIMULATED sweeps exactly like the ESP32 will |
| `serial_bridge.py` | Reads the ESP32's INA219 Serial printouts over USB and POSTs them to `/telemetry` (shown as the "Live sensor" strip in Live node mode). Close the Arduino Serial Monitor first. |

## Two views

- **Owner view** (`http://localhost:8000/`): Digital Twin & Health. A 3D twin of the panel (three.js is vendored in `web/vendor/`,
  so it works offline; falls back to a 2D drawing without WebGL), a 0-100 health score, live V/I/P, a probabilistic diagnosis,
  expected vs measured, money, recommended actions, a Simulation switch, and setup (baseline, calibration, sensors, panel profile).
- **Installer view** (`http://localhost:8000/console`): the diagnostic console (fleet, model, scan log, test bench).

Every number carries a badge: REAL, CALIBRATED, PROVISIONAL, SIMULATED or PHYSICS MODEL. Language switch (en/hi/kn) is shared.

Owner-view API: `GET /twin/state` (real), `POST /twin/sim` (simulated, via `/simulate`), `GET /twin/history`, `GET /twin/alerts`,
`GET /calibration`, `POST /calibration/verify` (`{meter_ma}`), `POST /calibration/reset`, `GET /sensors`,
`GET /profile`, `POST /profile`, `POST /profile/active`.

Data files: `data/calibration.json` (gain/offset, default 1.0/0, PROVISIONAL until verified against a multimeter),
`data/sensors.json` (LDR roles and pins: reference or a region), `data/panel_profile.json` (panel datasheet profile).
The real-data classifier (`sn/realdx.py`, trained on pvlib simulations of our single panel using Isc, Voc and the reference LDR)
keeps all its assumptions (priors, time-pattern likelihood ratios) in one config block and claims no accuracy for real data.
`serial_bridge.py` also parses LDR lines (`LDR1: 2345`, `LDR: 1200, 1800`) and `Temperature: 28.5 C` if the node prints them.

## Live mode (simple, no baseline, no jumper)

The default REAL live view needs nothing but the torch and the wiring as it is. `data/live_thresholds.json` holds every number:
the 2 s average of the panel current is >= 0.35 mA green (producing normally), 0.20 to 0.35 amber (partly shaded), below 0.20 red
(shaded); a new state must hold 1.5 s before it is shown; drop % = 1 - current / 0.5 mA (approximate, compared with full torch light).
Money uses `money()` with that drop and is labelled PROVISIONAL. Telegram alerts fire when the state goes amber or red and when it
returns to green (max one per 30 s per state). Response time is about 3.8 s with the 1.5 s hold on a 1 reading/s stream; lower
`hold_s` (0.5 gives about 2-3 s) for faster. Try it: shine the torch on the panel, cover half, then all, then take your hand away.

The baseline mode below is kept (Advanced section; set `"mode": "baseline"` in that file to use it for the live view).

## Real bench, baseline mode (one panel, INA219, USB serial)

No firmware change: `serial_bridge.py` only reads what the ESP32 already prints. Wiring: a jumper from INA219 VIN- to
panel minus = short circuit (V ~ 0, I = Isc); jumper removed = open circuit (I ~ 0, V = Voc).

1. Torch fixed, clean panel: jumper on (2 s), jumper off (2 s), press **Set baseline** in Live node mode.
2. Add the fault (dust, shade, loose joint).
3. Jumper on, jumper off again. The console shows the finding, the % change and the approximate loss.

Findings are rules on the change against the baseline (Isc down > 8% with Voc within 5%: less light; Voc down > 15%:
voltage loss). Loss = 1 - (Isc x Voc) / (baseline Isc x baseline Voc), an approximation, scaled to a kWp home for the rupees.
Curves are pvlib single-diode models through the measured Isc and Voc (tag: Physics model). Real-bench accuracy is not
measured yet.

API: `POST /telemetry`, `POST /bench/point`, `POST /bench/baseline`, `GET /bench/status`, `GET /bench/curves`, `GET /bench/events`.

## JSON contract for the ESP32 (teammate)

POST `http://<laptop-ip>:8000/sweeps` with `Content-Type: application/json`:

```json
{
  "node_id": "SN-001",
  "seq": 12,
  "source": "real",
  "temp_c": 34.2,
  "ref_isc_ma": 64.8,
  "points": [[0.081, 241.3], [0.95, 240.8], "...", [13.62, 0.0]],
  "fw": "0.1"
}
```

- `points`: `[volts, milliamps]`, 60-100 points from near short circuit to open circuit; the last point is
  Voc measured with the MOSFET off. Volts = INA219 bus voltage + shunt voltage.
- `ref_isc_ma`: reference cell current from INA219 #2. `temp_c`: DS18B20 on the back of panel B.
- Optional `mid_v`: one voltage per sweep point, same length as `points`: the voltage across panel A
  (the panel on the ground side), read by an ESP32 ADC pin through a divider on the junction between
  the two panels (for example 100k over 33k, so 14 V becomes about 3.3 V). With it the engine says
  WHICH panel is shaded or has the failed diode; without it, it says "one of the two panels".
- Reply: `{"led": "green|amber|red", "headline": ..., "confidence": ..., "loss_pct": ..., "action": ...}`
  so the node can set its LED.

## Honesty notes

- The model is trained on physics simulations of the panel twin. Anything from the Virtual bench or
  `virtual_node.py` is tagged SIMULATED in the dashboard.
- Real-bench accuracy is reported only after recording real sweeps, tested on sessions the model never saw.
