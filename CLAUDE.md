# SuryaNidaan – COLOSSUS 3.0 hackathon (24 h, Hardware/IoT track)

AI diagnostics for rooftop solar: sweep a panel string's I-V curve, classify the fault
(healthy, soiling, partial_shade, series_resistance, bypass_short), explain why, estimate % loss and Rs/day,
alert via Telegram. Judging: impact, creativity, design, execution. Live demo required.

## Stack (keep it; no new heavy dependencies)
Python, pvlib, scikit-learn, FastAPI + SQLite (api.py). Main UI: web/index.html (plain HTML/JS + Plotly, served at /). Backup: Streamlit (dashboard.py).
ESP32 (Arduino C++) + INA219 + MOSFET load + MCP4725 + DS18B20 posts JSON to POST /sweeps (see README).

## Layout
- sn/pv_sim.py: simulator (2 mini panels in series, bypass diodes, faults, INA219-like noise)
- sn/features.py: features shared by real and simulated curves
- sn/rules.py: rule baseline
- sn/model.py: calibrated RandomForest + IsolationForest + rules-vs-AI benchmark
- sn/engine.py: diagnose()
- sn/alerts.py: Telegram (sent in the console language)
- sn/i18n.py: all user-facing sentences in en/hi/kn. engine.diagnose() returns {key, p} pairs (verdict, location,
  act, ev) next to the English text; the console and Telegram render them. New sentences go in i18n.py, never
  hard-coded in the UI. Have a native speaker check hi/kn before the final.
- Optional payload field mid_v (volts across panel A per sweep point) lets engine.locate() name the faulty panel.
- virtual_node.py: posts simulated sweeps
- train.py: trains the model

## Run
python train.py ; uvicorn api:app --host 0.0.0.0 --port 8000 (console at http://localhost:8000) ; python virtual_node.py

## Hard rules
- Anything simulated must be labelled SIMULATED (payload source="sim"). Never present simulation as measurement.
- Never invent accuracy or field-validation claims; real-bench accuracy only from held-out real sessions.
- Prototype stays below 25 V. Never design anything for live rooftop strings.
- Make small, testable changes; run the app after each change; commit often (git history proves in-event work).

## Next tasks
1. Before 2 PM: make sure the demo runs smoothly; fix any errors on this laptop.
2. After 2 PM: help with the ESP32 sweep firmware matching the JSON contract; ingest real sweeps.
3. Tonight: fit the panel twin from real healthy curves (pvlib.ivtools.sde.fit_sandia_simple), retrain with real
   sessions using a split by session, then a vendor fleet tab (simulated sites labelled SIM).
