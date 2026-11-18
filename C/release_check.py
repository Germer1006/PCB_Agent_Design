"""Artifact-bound prototype release gate. Zero DRC alone never grants release."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from datetime import datetime

from constraints import REQUIRED_REVIEWS, read_constraints
from critical_routes import check_routes
from kicad_io import _xml_netlist, _sexpr_netlist, _enabled_flag
from project_files import CRITICAL_CHECKS


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(project):
    project = Path(project).resolve(strict=True)
    files = {}
    for path in sorted(project.rglob("*")):
        if path.is_symlink():
            raise ValueError("Deliverable must not depend on symlinks: " + str(path))
        if not path.is_file() or path.name.startswith("~") or path.suffix in {".kicad_prl", ".lck"}:
            continue
        files[str(path.relative_to(project)).replace("\\", "/")] = digest(path)
    return files


def read_expected(project):
    project = Path(project)
    path = project / "input.net.xml"
    if not path.is_file():
        path = project / "input.net"
    text = path.read_text(encoding="utf-8-sig")
    parts, nets = _xml_netlist(text) if text.lstrip().startswith("<") else _sexpr_netlist(text)
    parts = [p for p in parts if not p["ref"].startswith("#")
             and not _enabled_flag(p["properties"].get("exclude_from_board"))]
    return parts, nets


def check_readback(expected_parts, expected_nets, audit):
    expected = {p["ref"]: p for p in expected_parts}
    counts = Counter(p["ref"] for p in audit["components"])
    actual = {p["ref"]: p for p in audit["components"]}
    issues = []
    if set(actual) != set(expected) or any(n != 1 for n in counts.values()):
        issues.append("Component count/reference set differs from source netlist")
    for ref in set(actual) & set(expected):
        for field in ("footprint", "value"):
            if actual[ref][field] != expected[ref][field]:
                issues.append(f"{ref}: {field} differs from source netlist")
    assignments = {(node["ref"], node["pin"]): net["name"] for net in expected_nets
                   for node in net["nodes"] if node["ref"] in expected}
    actual_pins = {}
    for pad in audit["pads"]:
        key = pad["ref"], pad["number"]
        actual_pins.setdefault(key, set()).add(pad["net"])
        if pad["net"] != assignments.get(key, ""):
            issues.append(f"{key[0]}.{key[1]} net differs")
    for key, name in assignments.items():
        if actual_pins.get(key) != {name}:
            issues.append(f"{key[0]}.{key[1]} missing or inconsistent pad copies")
    if audit.get("copper_layers") != 2:
        issues.append("Expected exactly two copper layers")
    if audit.get("duplicate_uuids"):
        issues.append("Duplicate native PCB UUIDs")
    return {"status": "passed" if not issues else "failed", "issues": sorted(set(issues)),
            "component_count": len(actual), "expected_pin_assignments": len(assignments)}


def seal_verification(directory, project, board, drc, audit, layout, origins):
    directory, project, board = Path(directory), Path(project), Path(board)
    data = {"schema_version": "pcb-verification-1", "project": str(project.relative_to(directory)),
            "board": str(board.relative_to(project)), "artifacts_sha256": snapshot(project),
            "drc": drc, "audit": audit, "layout": layout, "footprint_origins": origins}
    report = directory / "DRC.json"
    data["drc_report_sha256"] = digest(report) if report.is_file() else None
    (directory / "verification.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def _safe_relative(base, value):
    if not isinstance(value, str) or Path(value).is_absolute():
        raise ValueError("Evidence path must be relative")
    path = (base / value).resolve(strict=True)
    if not path.is_relative_to(base):
        raise ValueError("Evidence path leaves deliverable")
    return path


def evaluate(directory, review_path=None, *, require_roundtrip=True):
    directory = Path(directory).resolve(strict=True)
    blocked = []
    def block(identity, detail):
        blocked.append({"id": identity, "detail": detail})
    try:
        v = json.loads((directory / "verification.json").read_text(encoding="utf-8"))
        if v.get("schema_version") != "pcb-verification-1":
            raise ValueError("Unsupported verification schema")
        project = _safe_relative(directory, v["project"])
        board = _safe_relative(project, v["board"])
        artifacts = snapshot(project)
        if artifacts != v["artifacts_sha256"]:
            block("ARTIFACT_CHANGED", "PCB, project, constraints, netlist or library changed; rerun verification")
        if v["drc"].get("board_sha256") != digest(board) or v["drc"].get("project_sha256") != digest(board.with_suffix(".kicad_pro")):
            block("DRC_INPUT_CHANGED", "DRC is not bound to this PCB and project")
        raw = directory / "DRC.json"
        if digest(raw) != v["drc_report_sha256"]:
            block("DRC_REPORT_CHANGED", "Raw DRC report changed")
        payload = json.loads(raw.read_text(encoding="utf-8-sig"))
        if str(payload.get("source", "")).replace("\\", "/").rsplit("/", 1)[-1] != board.name:
            block("DRC_SOURCE", "DRC report source does not match")
        sections = ("violations", "unconnected_items", "schematic_parity")
        if any(not isinstance(payload.get(k), list) for k in sections):
            block("DRC_FORMAT", "Incomplete native DRC report")
        elif any(payload[k] for k in sections):
            block("DRC_FINDINGS", "All errors, warnings, exclusions and unconnected items must be resolved")
        if v["drc"].get("status") != "passed" or not v["drc"].get("all_track_errors"):
            block("DRC_NOT_PASSED", "Native DRC is not clean or did not request all track errors")
        parts, nets = read_expected(project)
        readback = check_readback(parts, nets, v["audit"])
        if readback["status"] != "passed":
            block("READBACK_FAILED", "; ".join(readback["issues"]))
        config = json.loads(board.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
        settings = config.get("board", {}).get("design_settings", {})
        if not settings.get("rules") or settings.get("drc_exclusions"):
            block("RULES_INCOMPLETE", "Explicit board rules required, no exclusions")
        for name in CRITICAL_CHECKS:
            if settings.get("rule_severities", {}).get(name, "error") != "error":
                block("CHECK_DISABLED:" + name, "Critical check was downgraded or disabled")
        if not (project / "fp-lib-table").is_file() or not (project / "PCB_Project.pretty").is_dir():
            block("LIBRARY_NOT_FROZEN", "Project-owned footprint library required")
        constraint_file = project / "pcb_constraints.json"
        contract = read_constraints(constraint_file) if constraint_file.is_file() else None
        if contract is None:
            block("CONSTRAINTS_MISSING", "No electrical/layout handoff provided")
        else:
            upstream = contract["upstream"]
            if upstream["status"] not in {"ready_for_downstream", "ready_for_pcb", "verified"} or upstream.get("open_issues"):
                block("UPSTREAM_UNRESOLVED", "Upstream A/B issues cannot be closed by PCB DRC")
            for part in parts:
                record = contract.get("footprints", {}).get(part["ref"], {})
                original = v["footprint_origins"].get(part["ref"], part["footprint"])
                if record.get("status") != "verified" or record.get("footprint") != original:
                    block("FOOTPRINT:" + part["ref"], "Footprint still candidate, missing or not the reviewed part")
        review_path = Path(review_path) if review_path else directory / "release_review.json"
        review = json.loads(review_path.read_text(encoding="utf-8-sig")) if review_path.is_file() else {}
        review_valid = (review.get("schema_version") == "pcb-review-1" and
                        review.get("reviewed_artifacts_sha256") == artifacts and
                        isinstance(review.get("reviewer"), str) and bool(review["reviewer"].strip()))
        try:
            datetime.fromisoformat(review.get("reviewed_at", "").replace("Z", "+00:00"))
        except (ValueError, TypeError):
            review_valid = False
        decisions = review.get("decisions", {}) if review_valid else {}
        if not isinstance(decisions, dict):
            decisions = {}
        def approved(identity):
            record = decisions.get(identity, {})
            return (isinstance(record, dict) and record.get("status") == "approved"
                    and isinstance(record.get("notes"), str) and bool(record["notes"].strip())
                    and isinstance(record.get("evidence"), list) and bool(record["evidence"])
                    and all(isinstance(x, str) and x.strip() for x in record["evidence"]))
        required = set(REQUIRED_REVIEWS) | {"cad_reopen"} | set((contract or {}).get("required_reviews", []))
        for identity in sorted(required):
            if not approved(identity):
                block("REVIEW:" + identity, "Needs artifact-bound, dated engineering review with evidence")
        checks = {item["id"]: item for item in v["layout"].get("checks", []) if not item["id"].startswith("copper:")}
        if contract is not None:
            # Recompute copper checks for both current packages and older C deliveries.
            checks.update((item["id"], item) for item in check_routes(board, contract))
        for item in checks.values():
            if item["status"] == "failed":
                detail = item["detail"] if item["id"].startswith("copper:") else "Measured layout violates an explicit constraint"
                block("LAYOUT:" + item["id"], detail)
            elif item["status"] != "passed" and not approved(item["id"]):
                block("LAYOUT_REVIEW:" + item["id"], "Missing limit/evaluation; no automatic pass")
        if not checks:
            block("LAYOUT_UNASSESSED", "No power-layout checks performed")
        if require_roundtrip:
            roundtrip_path = directory / "roundtrip_check.json"
            roundtrip = json.loads(roundtrip_path.read_text(encoding="utf-8")) if roundtrip_path.is_file() else {}
            if not (roundtrip.get("status") == "passed" and roundtrip.get("artifacts_sha256") == artifacts
                    and roundtrip.get("readback", {}).get("status") == "passed"):
                block("PACKAGE_NOT_VERIFIED", "ZIP extraction/recheck missing or stale")
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
        block("VERIFICATION_INVALID", str(error))
        readback = {"status": "not_assessed"}
    ready = not blocked
    return {"schema_version": "pcb-release-1", "prototype_ready": ready,
            "production_ready": False, "production_scope": "Not implemented; bench and production qualification are separate",
            "status": "prototype_ready" if ready else "review_or_rework_required", "blockers": blocked,
            "readback": readback}


def review_template(directory):
    directory = Path(directory)
    proof = json.loads((directory / "verification.json").read_text(encoding="utf-8"))
    project = _safe_relative(directory.resolve(), proof["project"])
    contract_file = project / "pcb_constraints.json"
    contract = read_constraints(contract_file) if contract_file.is_file() else {}
    identities = set(REQUIRED_REVIEWS) | {"cad_reopen"} | set((contract or {}).get("required_reviews", []))
    identities.update(x["id"] for x in proof["layout"].get("checks", []) if x["status"] == "not_assessed")
    if contract:
        board = _safe_relative(project, proof["board"])
        identities.update(x["id"] for x in check_routes(board, contract) if x["status"] == "not_assessed")
    return {"schema_version": "pcb-review-1", "reviewer": "", "reviewed_at": "",
            "reviewed_artifacts_sha256": snapshot(project),
            "decisions": {identity: {"status": "not_assessed", "notes": "", "evidence": []} for identity in sorted(identities)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--write-review-template", type=Path)
    parser.add_argument("--write-status", action="store_true")
    args = parser.parse_args()
    if args.write_review_template:
        with args.write_review_template.open("x", encoding="utf-8") as stream:
            json.dump(review_template(args.directory), stream, ensure_ascii=False, indent=2)
    result = evaluate(args.directory, args.review)
    if args.write_status:
        (args.directory / "release_status.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["prototype_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
