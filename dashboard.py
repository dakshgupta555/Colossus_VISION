"""SuryaNidaan dashboard.

Run:  streamlit run dashboard.py
Tabs: Virtual bench (simulated what-if) | Live node (API) | Rules vs AI | History
"""
import json
import os
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

from sn import config as C, engine, pv_sim as S

st.set_page_config(page_title="SuryaNidaan", page_icon=None, layout="wide")
ROOT = os.path.dirname(os.path.abspath(__file__))
ACCENT, GREY = "#1f6feb", "#9aa4b2"


@st.cache_resource(show_spinner="Loading the diagnostic model (first run trains it, ~1 min)...")
def get_bundle():
    return engine.bundle()


get_bundle()

# ---------------- sidebar ----------------
st.sidebar.header("Money assumptions")
kwp = st.sidebar.number_input("Installation size (kWp)", 0.5, 100.0, C.DEFAULT_KWP, 0.5)
yld = st.sidebar.number_input("Yield (kWh per kWp per day)", 1.0, 7.0, C.DEFAULT_YIELD, 0.1)
tariff = st.sidebar.number_input("Tariff (Rs per kWh)", 1.0, 20.0, C.DEFAULT_TARIFF, 0.5)
st.sidebar.caption("The bench panels are tiny; the % loss is scaled to this installation.")
st.sidebar.header("Backend")
api = st.sidebar.text_input("API URL", "http://127.0.0.1:8000")

st.title("SuryaNidaan: AI diagnostics for rooftop solar")
st.caption("Reads the panel's I-V curve, names the fault, explains why, and prices the loss. "
           "Prototype for COLOSSUS 3.0.")


def curve_fig(points, expected, title_suffix=""):
    a = np.asarray(points, float)
    e = np.asarray(expected, float)
    fig = make_subplots(rows=1, cols=2, subplot_titles=("I-V curve", "P-V curve"), horizontal_spacing=0.1)
    fig.add_trace(go.Scatter(x=e[:, 0], y=e[:, 1], name="Expected healthy (twin)", mode="lines",
                             line=dict(color=GREY, dash="dash", width=2)), 1, 1)
    fig.add_trace(go.Scatter(x=a[:, 0], y=a[:, 1], name="Measured", mode="lines+markers",
                             line=dict(color=ACCENT, width=2.5), marker=dict(size=4)), 1, 1)
    fig.add_trace(go.Scatter(x=e[:, 0], y=e[:, 0] * e[:, 1], mode="lines", showlegend=False,
                             line=dict(color=GREY, dash="dash", width=2)), 1, 2)
    fig.add_trace(go.Scatter(x=a[:, 0], y=a[:, 0] * a[:, 1], mode="lines+markers", showlegend=False,
                             line=dict(color=ACCENT, width=2.5), marker=dict(size=4)), 1, 2)
    fig.update_xaxes(title_text="Voltage (V)", rangemode="tozero")
    fig.update_yaxes(title_text="Current (mA)", rangemode="tozero", row=1, col=1)
    fig.update_yaxes(title_text="Power (mW)", rangemode="tozero", row=1, col=2)
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=40, b=10),
                      legend=dict(orientation="h", y=-0.22), template="plotly_white")
    return fig


def render(res, points, source):
    if source == "sim":
        st.markdown(":orange-background[**SIMULATED**] curve from the physics twin, not a measurement")
    else:
        st.markdown(":green-background[**REAL MEASUREMENT**] from the SuryaNidaan node")
    text = f"**{res['headline']}**: {res['confidence'] * 100:.0f}% confidence"
    if res["status"] != "ok":
        st.warning(text)
    elif res["label"] == "healthy":
        st.success(text)
    else:
        st.error(text)
    left, right = st.columns([3, 2])
    with left:
        st.plotly_chart(curve_fig(points, res["expected_points"]))
    with right:
        c1, c2, c3 = st.columns(3)
        c1.metric("Power loss", f"{res['loss_pct']}%")
        c2.metric("Rs lost / day", f"{res['money']['inr_day']}")
        c3.metric("Rs lost / year", f"{res['money']['inr_year']:,}")
        m, e = res["measured"], res["expected"]
        st.dataframe(pd.DataFrame({
            "Measured": [m["isc_ma"], m["voc_v"], m["pmax_mw"], m["ff"]],
            "Expected": [e["isc_ma"], e["voc_v"], e["pmax_mw"], e["ff"]],
        }, index=["Isc (mA)", "Voc (V)", "Pmax (mW)", "Fill factor"]))
        st.caption(f"Light {res['light_pct']}% of full (reference cell) · panel {res['temp_c']} C · "
                   f"scaled to {res['assumptions']['kwp']} kWp at Rs {res['assumptions']['tariff_inr_per_kwh']}/kWh")
    st.markdown("**Why (evidence)**")
    for ev in res["evidence"]:
        st.markdown(f"- {ev}")
    st.markdown(f"**Recommended action:** {res['action']}")
    agree = "agrees" if res["rules_agree"] else "disagrees"
    st.caption(f"Physics-rule baseline says **{C.NICE[res['rule_label']]}** ({agree} with the AI). "
               + ("Curve is outside the training range." if res["out_of_distribution"] else ""))
    with st.expander("Class probabilities and features"):
        st.bar_chart(pd.Series({C.NICE[k]: v for k, v in res["probs"].items()}))
        st.json(res["features"])


tab_bench, tab_live, tab_ai, tab_hist = st.tabs(["Virtual bench (simulated)", "Live node", "Rules vs AI", "History"])

# ---------------- Virtual bench ----------------
with tab_bench:
    st.markdown("Inject faults into a physics simulation of our two-panel string and watch the diagnosis. "
                "**Everything on this tab is simulated.**")
    c1, c2, c3 = st.columns(3)
    g = c1.slider("Light level (% of full lamp)", 15, 110, 90) / 100
    temp = c1.slider("Panel temperature (C)", 25, 70, 38)
    shade = c2.slider("Shade on panel B (%)", 0, 95, 0) / 100
    soil = c2.slider("Dust on both panels (%)", 0, 60, 0) / 100
    rx = c3.slider("Loose connector: extra resistance (ohm)", 0.0, 25.0, 0.0, 0.5)
    short = c3.checkbox("Panel B bypass diode failed short")
    noisy = c3.checkbox("Add sensor noise", value=True)
    if "seed" not in st.session_state:
        st.session_state.seed = 1
    b1, b2 = st.columns([1, 5])
    if b1.button("Rescan"):
        st.session_state.seed += 1
    scn = dict(g=g, temp_c=temp, iph_scale=[1.0, 1.0], rs_scale=[1.0, 1.0], soiling=soil,
               shade=shade, shade_panel=1, extra_r=rx, shorted=1 if short else None)
    payload = S.simulate(scn, np.random.default_rng(st.session_state.seed), noise=noisy)
    res = engine.diagnose(payload, kwp=kwp, yld=yld, tariff=tariff)
    render(res, payload["points"], "sim")
    if b2.button("Send this sweep through the pipeline (API, database, Telegram)"):
        body = dict(payload, node_id="SN-SIM-01", seq=int(time.time()), source="sim", fw="bench",
                    kwp=kwp, yield_kwh_per_kwp_day=yld, tariff=tariff)
        try:
            r = requests.post(api + "/sweeps", json=body, timeout=10).json()
            st.toast(f"Stored as #{r['id']} · LED {r['led']} · alert {r['alert']}")
        except Exception as exc:
            st.error(f"API not reachable at {api}: {exc}")


# ---------------- Live node ----------------
def live_view():
    try:
        latest = requests.get(api + "/latest", timeout=3).json()
    except Exception as exc:
        st.error(f"API not reachable at {api}. Start it with: uvicorn api:app --host 0.0.0.0 --port 8000 ({exc})")
        return
    if not latest:
        st.info("No sweeps yet. Start the ESP32 node or run: python virtual_node.py")
        return
    age = time.time() - latest["ts"]
    st.caption(f"Sweep #{latest['id']} from **{latest['node_id']}** · {age:.0f} s ago · refreshes every 2 s")
    render(latest["result"], latest["payload"]["points"], latest["source"])


with tab_live:
    if hasattr(st, "fragment"):
        st.fragment(run_every=2)(live_view)()
    else:
        st.button("Refresh")
        live_view()

# ---------------- Rules vs AI ----------------
with tab_ai:
    path = os.path.join(ROOT, "models", "benchmark.json")
    if not os.path.exists(path):
        st.info("Run python train.py to create the benchmark.")
    else:
        bench = json.load(open(path))
        rows = []
        for name in ["standard", "hard"]:
            rows.append(dict(**{"Test set": name, "Rules macro-F1": bench[name]["rules"]["macro_f1"],
                                "AI macro-F1": bench[name]["ai"]["macro_f1"]}))
        st.markdown("**Same unseen curves, two diagnosers.** The physics rules work in normal conditions; "
                    "the AI holds up in dim light, heat and mild faults.")
        st.dataframe(pd.DataFrame(rows).set_index("Test set"))
        st.caption(bench["note"])
        names = [C.NICE[c].split(" (")[0] for c in bench["classes"]]
        c1, c2 = st.columns(2)
        for col, who in [(c1, "rules"), (c2, "ai")]:
            cm = np.array(bench["hard"][who]["confusion"])
            fig = go.Figure(go.Heatmap(z=cm, x=names, y=names, colorscale="Blues", showscale=False,
                                       text=cm, texttemplate="%{text}"))
            fig.update_layout(title=f"Hard cases: {'Rules' if who == 'rules' else 'AI'} (rows = true fault)",
                              height=380, margin=dict(l=10, r=10, t=50, b=10), yaxis_autorange="reversed",
                              xaxis_title="Predicted", template="plotly_white")
            col.plotly_chart(fig)
        imp = get_bundle().get("importances", {})
        if imp:
            top = pd.Series(imp).head(10)[::-1]
            fig = go.Figure(go.Bar(x=top.values, y=top.index, orientation="h", marker_color=ACCENT))
            fig.update_layout(title="What the model looks at (feature importance)", height=360,
                              margin=dict(l=10, r=10, t=50, b=10), template="plotly_white")
            st.plotly_chart(fig)

# ---------------- History ----------------
with tab_hist:
    try:
        hist = requests.get(api + "/history?limit=100", timeout=3).json()
        if hist:
            df = pd.DataFrame(hist)
            df["time"] = pd.to_datetime(df["ts"], unit="s").dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata").dt.strftime("%H:%M:%S")
            df["source"] = df["source"].map({"sim": "SIMULATED", "real": "REAL"}).fillna(df["source"])
            st.dataframe(df[["id", "time", "node_id", "source", "headline", "confidence", "loss_pct",
                             "inr_day", "rule_label"]], hide_index=True)
        else:
            st.info("No sweeps stored yet.")
    except Exception as exc:
        st.error(f"API not reachable at {api}: {exc}")
