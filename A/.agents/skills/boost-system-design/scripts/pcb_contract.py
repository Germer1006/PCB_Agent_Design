"""Emit qualitative, versioned PCB intent; never invent millimetre limits."""
import re


def make_pcb_constraints(spec):
    refs = set()
    for item in spec["components"] + spec.get("generic_passives", []):
        refs.update(r for r in item["designator"].split(",") if re.fullmatch(r"[A-Z]+[0-9]+", r))
    sources = {
        "boost": "https://www.ti.com/lit/an/snva731/snva731.pdf#page=8",
        "driver": "https://www.ti.com/lit/ds/symlink/lm5114.pdf#page=18",
    }
    groups = []
    for identity, members, source in (
        ("boost_power_cluster", ["Q1", "D1", "L1", "C10"], sources["boost"]),
        ("gate_driver_cluster", ["U2", "R7", "Q1"], sources["driver"]),
        ("shunt_sense_cluster", ["R1", "U3"], "design_spec.json#pcb_guidance"),
    ):
        available = [r for r in members if r in refs]
        if len(available) > 1:
            groups.append({"id": identity, "refs": available, "max_span_mm": None,
                           "weight": 6.0, "evidence": [source]})
    return {
        "schema_version": "pcb-constraints-1", "design_revision": spec["revision"],
        "upstream": {"status": spec["status"], "open_issues": list(spec.get("open_issues", []))},
        "footprints": {}, "groups": groups, "pad_pairs": [], "fixed": {}, "regions": [], "keepouts": [],
        "required_reviews": ["driver_decoupling", "mcu_decoupling", "switching_loop", "kelvin_sampling", "startup_protection"],
        "notes": ["B must bind decoupling capacitors to verified physical pins and add footprint evidence.",
                  "null limits require review; center distances do not verify routed loops or Kelvin topology.",
                  "Preserve one GND net and continuous return paths; hardware/firmware protection remains upstream work."],
    }
