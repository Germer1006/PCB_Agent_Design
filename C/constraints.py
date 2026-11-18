"""Versioned A/B -> C constraints; geometry checks do not certify power integrity."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

VERSION = "pcb-constraints-1"
REQUIRED_REVIEWS = ("electrical", "power_layout", "sampling_return", "mechanical", "manufacturing_process")


def _keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"{label}: expected object with known fields {sorted(allowed)}")


def _number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label}: finite number required")
    if positive and value <= 0:
        raise ValueError(f"{label}: must be positive")


def _strings(value, label, nonempty=False):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f"{label}: string array required")
    if nonempty and not value:
        raise ValueError(f"{label}: evidence/identifiers must not be empty")


def validate_constraints(data, design=None):
    """Reject typos, invented pins, invalid limits and unsupported versions."""
    _keys(data, {"schema_version", "design_revision", "upstream", "footprints", "groups", "pad_pairs",
                 "fixed", "regions", "keepouts", "required_reviews", "notes"}, "constraints")
    if data.get("schema_version") != VERSION:
        raise ValueError("Unsupported PCB constraint schema_version")
    if not isinstance(data.get("design_revision"), str) or not data["design_revision"].strip():
        raise ValueError("design_revision required")
    upstream = data.get("upstream", {})
    _keys(upstream, {"status", "open_issues", "source_sha256"}, "upstream")
    if not isinstance(upstream.get("status"), str):
        raise ValueError("upstream.status required")
    _strings(upstream.get("open_issues", []), "upstream.open_issues")
    footprints = data.get("footprints", {})
    if not isinstance(footprints, dict):
        raise ValueError("footprints must be an object")
    refs = {c["ref"] for c in design["components"]} if design else None
    pins = {(p["ref"], p["number"]): p["net"] for c in design["components"] for p in c["pads"]} if design else None
    for ref, record in footprints.items():
        _keys(record, {"status", "footprint", "mpn", "reviewer", "evidence"}, f"footprints.{ref}")
        if record.get("status") not in {"candidate", "verified", "unknown"}:
            raise ValueError(f"{ref}: unsupported footprint status")
        if refs is not None and ref not in refs:
            raise ValueError(f"Unknown component {ref}")
        _strings(record.get("evidence", []), f"{ref}.evidence")
        if record.get("status") == "verified" and not (
            all(isinstance(record.get(k), str) and record[k].strip() for k in ("footprint", "mpn", "reviewer"))
            and record.get("evidence")
        ):
            raise ValueError(f"{ref}: verified needs footprint, mpn, reviewer and evidence")
    ids = set()
    def common(item, allowed):
        _keys(item, allowed, "layout constraint")
        identity = item.get("id")
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ValueError("Missing/duplicate constraint id")
        ids.add(identity)
        _strings(item.get("evidence", []), identity + ".evidence", True)
        weight = item.get("weight", 5.0)
        _number(weight, identity + ".weight", True)
        return identity
    for section in ("groups", "pad_pairs", "regions", "keepouts"):
        if not isinstance(data.get(section, []), list):
            raise ValueError(section + " must be an array")
    for item in data.get("groups", []):
        identity = common(item, {"id", "refs", "max_span_mm", "weight", "evidence"})
        _strings(item.get("refs"), identity + ".refs", True)
        if len(set(item["refs"])) != len(item["refs"]) or len(item["refs"]) < 2:
            raise ValueError(identity + ": group needs distinct components")
        if refs is not None and set(item["refs"]) - refs:
            raise ValueError(identity + ": unknown group component")
        if item.get("max_span_mm") is not None:
            _number(item["max_span_mm"], identity + ".max_span_mm", True)
    for item in data.get("pad_pairs", []):
        identity = common(item, {"id", "a", "b", "kind", "max_distance_mm", "weight", "evidence"})
        if item.get("kind") not in {"decoupling", "gate_drive", "kelvin", "power_loop", "proximity"}:
            raise ValueError(identity + ": unsupported pad pair kind")
        for tag in ("a", "b"):
            endpoint = item.get(tag)
            _strings(endpoint, identity + "." + tag)
            if len(endpoint) != 2 or (pins is not None and tuple(endpoint) not in pins):
                raise ValueError(identity + ": unknown pad endpoint")
        if item["a"] == item["b"]:
            raise ValueError(identity + ": endpoints must differ")
        if pins is not None and item["kind"] in {"decoupling", "kelvin"}:
            if not pins[tuple(item["a"])] or pins[tuple(item["a"])] != pins[tuple(item["b"])]:
                raise ValueError(identity + ": decoupling/Kelvin pair must refer to the same actual net")
        if item.get("max_distance_mm") is not None:
            _number(item["max_distance_mm"], identity + ".max_distance_mm", True)
    for section in ("regions", "keepouts"):
        for item in data.get(section, []):
            identity = common(item, {"id", "refs", "bounds_mm", "weight", "evidence"})
            _strings(item.get("refs"), identity + ".refs", section == "regions")
            if refs is not None and set(item["refs"]) - refs:
                raise ValueError(identity + ": unknown component")
            bounds = item.get("bounds_mm")
            if not isinstance(bounds, list) or len(bounds) != 4:
                raise ValueError(identity + ": rectangular bounds required")
            for v in bounds:
                _number(v, identity + ".bounds_mm")
            if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
                raise ValueError(identity + ": invalid rectangle")
    fixed = data.get("fixed", {})
    if not isinstance(fixed, dict):
        raise ValueError("fixed must be an object")
    for ref, location in fixed.items():
        _keys(location, {"x", "y", "angle"}, "fixed." + ref)
        if refs is not None and ref not in refs:
            raise ValueError("Unknown fixed component " + ref)
        for tag in ("x", "y", "angle"):
            _number(location.get(tag), ref + "." + tag)
        if location["angle"] not in (0, 90, 180, 270):
            raise ValueError("Fixed rotations must be orthogonal")
    _strings(data.get("required_reviews", list(REQUIRED_REVIEWS)), "required_reviews")
    _strings(data.get("notes", []), "notes")
    return copy.deepcopy(data)


def read_constraints(path, design=None):
    if path is None:
        return None
    return validate_constraints(json.loads(Path(path).read_text(encoding="utf-8-sig")), design)


def attach_profile(profile, constraints):
    profile["constraints"] = constraints or {}
    for pair in (constraints or {}).get("pad_pairs", []):
        profile["pairs"].append((tuple(pair["a"]), tuple(pair["b"]), pair.get("weight", 5.0)))
    return profile


def envelope(part, location):
    from placement import rotate
    corners = [rotate(x, y, location["angle"]) for x in (part["min_x"], part["max_x"])
               for y in (part["min_y"], part["max_y"])]
    return (location["x"] + min(x for x, y in corners), location["y"] + min(y for x, y in corners),
            location["x"] + max(x for x, y in corners), location["y"] + max(y for x, y in corners))


def check_layout(design, placement, constraints):
    from placement import placed_pads
    if constraints is None:
        return {"status": "needs_review", "checks": [{"id": "constraints_missing", "status": "not_assessed",
                "detail": "No power-layout constraints supplied; DRC does not replace this review."}]}
    constraints = validate_constraints(constraints, design)
    if set(placement) != {p["ref"] for p in design["components"]}:
        raise ValueError("Placement component set differs from netlist")
    endpoints = {(p["ref"], p["number"]): (p["x"], p["y"]) for p in placed_pads(design, placement)}
    parts = {p["ref"]: p for p in design["components"]}
    boxes = {ref: envelope(parts[ref], pos) for ref, pos in placement.items()}
    findings = []
    def result(identity, value, limit, description):
        findings.append({"id": identity, "status": "not_assessed" if limit is None else
                         ("passed" if value <= limit + 1e-8 else "failed"),
                         "measured_mm": round(value, 6), "limit_mm": limit, "detail": description})
    for pair in constraints.get("pad_pairs", []):
        result(pair["id"], math.dist(endpoints[tuple(pair["a"])], endpoints[tuple(pair["b"])]),
               pair.get("max_distance_mm"), "Straight pad-to-pad distance; not routed length or loop inductance.")
    for group in constraints.get("groups", []):
        locations = [placement[ref] for ref in group["refs"]]
        span = math.hypot(max(p["x"] for p in locations) - min(p["x"] for p in locations),
                          max(p["y"] for p in locations) - min(p["y"] for p in locations))
        result(group["id"], span, group.get("max_span_mm"), "Component-center bounding-box diagonal.")
    for section in ("regions", "keepouts"):
        for rule in constraints.get(section, []):
            r = rule["bounds_mm"]
            for ref in rule["refs"] or sorted(boxes):
                a = boxes[ref]
                good = (a[0] >= r[0] and a[1] >= r[1] and a[2] <= r[2] and a[3] <= r[3]) if section == "regions" else (
                    a[2] <= r[0] or r[2] <= a[0] or a[3] <= r[1] or r[3] <= a[1])
                findings.append({"id": rule["id"] + ":" + ref, "status": "passed" if good else "failed",
                                 "detail": "Component envelope only; this does not check copper keepouts."})
    for ref, expected in constraints.get("fixed", {}).items():
        good = all(abs(placement[ref][key] - value) < 1e-6 for key, value in expected.items())
        findings.append({"id": "fixed:" + ref, "status": "passed" if good else "failed"})
    statuses = {r["status"] for r in findings}
    status = "failed" if "failed" in statuses else "needs_review" if "not_assessed" in statuses or not findings else "passed"
    return {"status": status, "checks": findings, "scope": "Placement geometry; review actual current loops, Kelvin topology and return paths separately."}
