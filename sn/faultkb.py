"""Fault knowledge base (sn/fault_kb.json): one entry per fault. Every alert card and every report reads from it."""
import json
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sn", "fault_kb.json")
_kb = {}


def load():
    if not _kb:
        _kb.update(json.load(open(PATH)))
    return _kb


def faults():
    return load()["faults"]


def recipients():
    return load()["recipients"]


def id_for_kind(kind):
    """Maintenance-queue kind -> knowledge-base id (unknown kinds fall back to 'unexpected_readings', never to nothing)."""
    for fid, e in faults().items():
        if kind in e["maps_from"]:
            return fid
    return "unexpected_readings"


def entry(fid):
    return faults()[fid]
