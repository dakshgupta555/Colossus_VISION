"""Print-ready HTML fault report (A4 print CSS). "Download PDF" is the browser's Save as PDF: no PDF library.

Honest by construction: preliminary observations only, every number tagged REAL or SIMULATED, no claims of defects, warranty
eligibility or violations, and the authority version describes observations and asks for investigation without blaming anyone.
"""
import html
import time

from . import faultkb, i18n

e = html.escape


def svg_line(series, w=520, h=150, color=("#1E4E9A", "#8A949E"), xlabel="", ylabel=""):
    """series: list of lists of (x, y). Returns an inline SVG polyline chart."""
    pts = [p for s in series for p in s]
    if len(pts) < 2:
        return ""
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = 0.0, max(max(p[1] for p in pts), 1e-9) * 1.1
    L, R, T, B = 42, 8, 8, 26
    sx = lambda x: L + (x - x0) / ((x1 - x0) or 1) * (w - L - R)
    sy = lambda y: h - B - (y - y0) / ((y1 - y0) or 1) * (h - T - B)
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg" role="img">']
    for k in range(5):
        yv = y0 + (y1 - y0) * k / 4
        out.append(f'<line x1="{L}" x2="{w-R}" y1="{sy(yv):.1f}" y2="{sy(yv):.1f}" stroke="#E3E7EE"/><text x="{L-5}" y="{sy(yv)+4:.1f}" font-size="10" text-anchor="end" fill="#5A6678">{yv:.2g}</text>')
    for i, s in enumerate(series):
        d = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in s)
        out.append(f'<polyline points="{d}" fill="none" stroke="{color[i % len(color)]}" stroke-width="2" {"stroke-dasharray=\"5 4\"" if i == 1 else ""}/>')
    out.append(f'<text x="{L}" y="{h-6}" font-size="10" fill="#5A6678">{e(xlabel)}</text><text x="{L+6}" y="11" font-size="10" fill="#5A6678">{e(ylabel)}</text></svg>')
    return "".join(out)


def svg_bars(labels, values, w=520, h=150, unit="%"):
    if not values:
        return ""
    top = max(max(values), 1e-9) * 1.15
    L, B = 30, 30
    bw = (w - L - 10) / len(values)
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg" role="img">']
    for i, (lab, v) in enumerate(zip(labels, values)):
        bh = (h - B - 14) * v / top
        x = L + i * bw + 4
        out.append(f'<rect x="{x:.1f}" y="{h-B-bh:.1f}" width="{max(4, bw-8):.1f}" height="{bh:.1f}" fill="#1E4E9A"/><text x="{x+max(4,bw-8)/2:.1f}" y="{h-B-bh-3:.1f}" font-size="9" text-anchor="middle" fill="#14202B">{v:.0f}{unit}</text>'
                   f'<text x="{x+max(4,bw-8)/2:.1f}" y="{h-12}" font-size="9" text-anchor="middle" fill="#5A6678">{e(str(lab))}</text>')
    return "".join(out) + "</svg>"


def _chart(ev, t):
    c = ev.get("chart")
    if not c:
        return f'<p class="muted">{e(t("rpt.noseries"))}</p>'
    if c["type"] == "line":
        svg = svg_line([c["series"]], xlabel=c.get("xlabel", ""), ylabel=c.get("ylabel", ""))
    elif c["type"] == "iv":
        svg = svg_line([c["measured"], c["expected"]], xlabel="V", ylabel="mA")
    else:
        svg = svg_bars(c["labels"], c["values"], unit=c.get("unit", "%"))
    cap = t(c["caption"]) if c.get("caption") else ""
    return f'<div class="chart">{svg}</div><p class="muted">{e(cap)}</p>'


def build(lang, recipient, fault, ev, report_id, now, ticket=None):
    """fault: dict(fault_id, node, location, kb(entry id), item, card, steps_text, draft). ev: evidence dict."""
    t = lambda k, p=None: i18n.t(lang, k, p)
    kb = faultkb.entry(fault["kb"])
    when = time.strftime("%d %b %Y, %H:%M:%S", time.localtime(now))
    src = ev.get("source", "SIMULATED")
    tag = lambda s: f'<span class="tag {s}">{e(t("tag." + ("real" if s == "REAL" else "sim")) if s in ("REAL", "SIMULATED") else s)}</span>'
    rows = "".join(f'<tr><td>{e(t(r["label"]))}</td><td class="n">{e(str(r["value"]))}</td><td>{e(r.get("unit", ""))}</td><td>{e(r.get("time", ""))}</td><td>{tag(r.get("source", src))}</td></tr>' for r in ev["rows"])
    li = lambda keys: "".join(f"<li>{e(t(k))}</li>" for k in keys)
    obs = "".join(f'<li>{e(t(x["key"], x["p"]))}</li>' for x in fault["card"]["evidence"])
    further = [t("rpt.further.cat", dict(cat=t("rec." + kb["service_category"])))] + ([t(kb["sensor_note"])] if kb.get("sensor_note") else [])
    summary = t("rpt.sum", dict(node=fault["node"], fault=t(kb["name"]), desc=t(kb["description"]), level=t("lvl." + kb["evidence_level"]), src=t("tag." + ("real" if src == "REAL" else "sim"))))
    d = fault["draft"]
    note = t("rpt.auth.note") if recipient in ("authority", "manufacturer") else ""
    return f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SuryaNidaan {e(report_id)}</title>
<style>
@page{{size:A4;margin:15mm 14mm}}
*{{box-sizing:border-box}}body{{margin:0;background:#EEF1F6;color:#14202B;font:13.5px/1.5 "Avenir Next","Segoe UI","Noto Sans","Kohinoor Devanagari","Noto Sans Devanagari","Noto Sans Kannada","Kannada Sangam MN",system-ui,sans-serif}}
.bar{{position:sticky;top:0;background:#14202B;color:#fff;padding:8px 18px;display:flex;gap:12px;align-items:center;font-size:13px}}.bar button{{background:#E8A100;border:0;border-radius:6px;padding:6px 14px;font-weight:700;cursor:pointer}}
.page{{max-width:210mm;margin:14px auto;background:#fff;padding:14mm;box-shadow:0 1px 8px rgba(0,0,0,.12)}}
header{{display:flex;justify-content:space-between;gap:16px;border-bottom:3px solid #E8A100;padding-bottom:8px;margin-bottom:10px}}
.brand{{font-size:22px;font-weight:800}}.brand small{{display:block;font-size:12px;font-weight:500;color:#5A6678}}
dl.meta{{display:grid;grid-template-columns:auto 1fr;gap:1px 10px;margin:0;font-size:12.5px}}dl.meta dt{{color:#5A6678}}dl.meta dd{{margin:0;font-weight:600}}
h2{{font-size:14px;margin:14px 0 4px;border-bottom:1px solid #DDE2EA;padding-bottom:2px;break-after:avoid}}h3{{font-size:12.5px;margin:8px 0 2px;color:#3A4A66}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}td,th{{border-bottom:1px solid #E6E9EF;padding:3px 6px;text-align:left}}th{{color:#5A6678;font-size:11.5px}}td.n{{text-align:right;font-variant-numeric:tabular-nums}}
ul{{margin:2px 0 0;padding-left:18px}}.muted{{color:#5A6678;font-size:12px;margin:2px 0}}.tag{{font-size:10px;font-weight:800;padding:1px 6px;border-radius:4px;color:#fff}}.tag.REAL{{background:#14202B}}.tag.SIMULATED{{background:#6B4FBB}}
.lvl{{display:inline-block;font-weight:800;font-size:11px;padding:2px 8px;border-radius:5px;border:1px solid #14202B}}
.letter{{border:1px solid #DDE2EA;border-radius:6px;padding:8px 12px;white-space:pre-wrap;background:#FAFBFD}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}section{{break-inside:avoid}}
footer{{margin-top:16px;border-top:1px solid #DDE2EA;padding-top:6px;font-size:11.5px;color:#5A6678}}
@media print{{body{{background:#fff}}.bar{{display:none}}.page{{box-shadow:none;margin:0;padding:0;max-width:none}}}}
</style></head><body>
<div class="bar"><span>SuryaNidaan · {e(report_id)}</span><button onclick="window.print()">{e(t("rpt.print"))}</button><span>{e(t("rpt.print.hint"))}</span></div>
<div class="page">
<header><div class="brand">SuryaNidaan<small>{e(t("rpt.title"))} · {e(t("rec." + recipient))}</small></div>
<dl class="meta"><dt>{e(t("rpt.id"))}</dt><dd>{e(report_id)}</dd><dt>{e(t("rpt.time"))}</dt><dd>{e(when)}</dd><dt>{e(t("rpt.fault_id"))}</dt><dd>{e(fault["fault_id"])}{(" · " + ticket["code"]) if ticket else ""}</dd>
<dt>{e(t("rpt.node"))}</dt><dd>{e(fault["node"])}</dd><dt>{e(t("rpt.location"))}</dt><dd>{e(fault["location"])}</dd><dt>{e(t("rpt.source"))}</dt><dd>{tag(src)}</dd></dl></header>
<section><h2>{e(t("rpt.summary"))}</h2><p>{e(summary)} <span class="lvl">{e(t("lvl." + kb["evidence_level"]))}</span></p></section>
<section><h2>{e(t("rpt.observed"))}</h2><p>{e(t(fault["card"]["what"]["key"], fault["card"]["what"]["p"]))}</p></section>
<section><h2>{e(t("rpt.evidence"))}</h2><table><thead><tr><th>{e(t("rpt.col.item"))}</th><th class="n">{e(t("rpt.col.value"))}</th><th>{e(t("rpt.col.unit"))}</th><th>{e(t("rpt.col.time"))}</th><th>{e(t("rpt.col.source"))}</th></tr></thead><tbody>{rows}</tbody></table>{_chart(ev, t)}</section>
<section><h2>{e(t("rpt.tech"))}</h2><div class="two"><div><h3>{e(t("rpt.t.observed"))}</h3><ul>{obs}</ul><h3>{e(t("rpt.t.suggested"))}</h3><ul>{li(kb["causes"])}</ul></div>
<div><h3>{e(t("rpt.t.unverified"))}</h3><ul>{li(kb["confirm"])}</ul><h3>{e(t("rpt.t.further"))}</h3><ul>{"".join(f"<li>{e(x)}</li>" for x in further)}</ul></div></div></section>
<section><h2>{e(t("rpt.resolution"))}</h2><ol>{li(kb["steps"])}</ol><p><b>{e(t("rpt.followup"))}:</b> {e(t(kb["follow_up"]))}</p><h3>{e(t("rpt.safety"))}</h3><ul>{li(kb["safety"])}</ul></section>
<section><h2>{e(t("rpt.letter"))}</h2><p class="muted">{e(t("rpt.letter.to", dict(to=t("rec." + recipient))))}</p><div class="letter"><b>{e(d["subject"])}</b>

{e(d["body"])}</div></section>
<footer><p><b>{e(t("rpt.limits"))}:</b> {e(t("rpt.limits.text"))} {e(note)}</p><p><b>{e(t("rpt.prelim"))}</b></p><p>SuryaNidaan · {e(t("foot.byline"))}</p></footer>
</div></body></html>"""
