"""Service directory, ticket stages, verification rule and the knowledge base."""
import json
import sqlite3

from sn import faultkb, i18n, resolve as R


def mem():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, site TEXT, label TEXT, sweep_id INTEGER, status TEXT)")
    R.init(con)
    return con


def test_directory_starts_empty_and_ranks_by_category_then_area():
    con = mem()
    assert R.contacts(con) == [] and R.match(con, "technician", "Bengaluru") == []
    R.contact_add(con, dict(name="Anywhere", category="technician"))
    R.contact_add(con, dict(name="Mysuru Tech", category="technician", area="Mysuru"))
    R.contact_add(con, dict(name="Bengaluru Tech", category="technician", area="Bengaluru"))
    R.contact_add(con, dict(name="Cleaner", category="cleaning", area="Bengaluru"))
    assert [c["name"] for c in R.match(con, "technician", "Jayanagar, Bengaluru")] == ["Bengaluru Tech", "Anywhere", "Mysuru Tech"]
    assert R.match(con, "authority", "x") == []


def test_ticket_stages_keep_history_and_drafts_are_never_marked_sent():
    con = mem()
    t = R.ticket_create(con, "k", "N1", "soiling")
    for s in ("report_generated", "contacted", "inspection", "awaiting_verification", "resolved"):
        R.set_stage(con, t["id"], s, "note")
    assert [h["stage"] for h in R.history(con, t["id"])][-1] == "detected" and R.ticket_get(con, tid=t["id"])["stage"] == "resolved"
    d = R.draft("en", "technician", dict(node="N1", fault="Dust", when="now", obs="o", steps="1. a"), dict(name="X", email="a@b.c", phone="+91 1"))
    assert d["mailto"].startswith("mailto:") and d["tel"] == "tel:+911" and "sent" not in d["body"].lower()


def test_verified_by_measurement_only_after_green_for_10_s():
    v = dict(before_ma=0.05, started=100.0, green_s=10, window_s=120)
    assert R.verify_decision(v, 105, "green", 103, 0.5)[0] == "waiting"
    assert R.verify_decision(v, 114, "green", 103, 0.5)[0] == "verified"
    assert R.verify_decision(v, 114, "amber", None, 0.2)[0] == "waiting"
    assert R.verify_decision(v, 230, "amber", None, 0.2)[0] == "not_verified"


def test_every_fault_has_a_next_step_in_all_languages_and_honest_sensor_notes():
    kb = faultkb.faults()
    assert len(kb) >= 7
    for fid, e in kb.items():
        assert e["steps"] and e["causes"] and e["confirm"] and e["follow_up"] and e["safety"] and e["service_category"] in faultkb.recipients()
        assert e["evidence_level"] in ("CONFIRMED", "SUSPECTED", "POSSIBLE SENSOR ERROR")
        keys = [e["name"], e["description"], e["follow_up"]] + e["causes"] + e["confirm"] + e["steps"] + e["safety"] + ([e["sensor_note"]] if e["sensor_note"] else [])
        for lang in ("en", "hi", "kn"):
            assert all(i18n.STRINGS[lang].get(k) for k in keys), (fid, lang)
    assert kb["bypass_diode"]["sensor_note"] and kb["loose_connection"]["sensor_note"]
    text = json.dumps({k: i18n.EN[k] for k in i18n.EN if k.startswith("kb.")}).lower()
    assert "open the junction" not in text and "tighten" not in text
