"""Validate B netlist and bind A's PCB intent to physical B pins. No footprint guessing."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "C"))
from constraints import VERSION, validate_constraints
from kicad_io import _xml_netlist, _sexpr_netlist, _enabled_flag, _parse, _items
from project_files import _copy_schematic
from verify import find_kicad_cli


def _read_netlist(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    parts, nets = _xml_netlist(text) if text.lstrip().startswith("<") else _sexpr_netlist(text)
    parts = [p for p in parts if not p["ref"].startswith("#") and not _enabled_flag(p["properties"].get("exclude_from_board"))]
    return parts, nets


def _identity(parts, nets):
    return ({p["ref"]: (p["value"], p["footprint"]) for p in parts},
            {(n["ref"], n["pin"]): net["name"] for net in nets for n in net["nodes"]})


def erc_violation_count(report):
    """Read both flat and sheet-scoped KiCad JSON. Unknown shapes cannot pass."""
    if not isinstance(report, dict):
        return None
    sections = []
    if "violations" in report:
        sections.append(report["violations"])
    if "sheets" in report:
        sheets = report["sheets"]
        if not isinstance(sheets, list) or not sheets or any(
            not isinstance(sheet, dict) or "violations" not in sheet for sheet in sheets
        ):
            return None
        sections.extend(sheet["violations"] for sheet in sheets)
    if not sections or any(
        not isinstance(section, list) or any(not isinstance(v, dict) for v in section)
        for section in sections
    ):
        return None
    return sum(map(len, sections))


def prepare(spec_path, netlist, output, *, schematic=None, footprint_evidence=None, bindings=None, cli_path=None):
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output must be a new/empty revision directory")
    spec_path, netlist = Path(spec_path).resolve(strict=True), Path(netlist).resolve(strict=True)
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    if not isinstance(spec.get("revision"), str) or not isinstance(spec.get("status"), str):
        raise ValueError("A design_spec revision/status required")
    parts, nets = _read_netlist(netlist)
    refs = [p["ref"] for p in parts]
    errors = []
    if not refs or len(set(refs)) != len(refs) or not all(refs):
        errors.append("B contains missing/duplicate component references")
    missing = [p["ref"] for p in parts if not p["footprint"]]
    if missing:
        errors.append("Footprints unassigned: " + ", ".join(missing))
    pin_nets = {}
    for net in nets:
        for node in net["nodes"]:
            if node["ref"] not in refs:
                errors.append("Unknown netlist reference " + node["ref"])
            key = node["ref"], node["pin"]
            if key in pin_nets and pin_nets[key] != net["name"]:
                errors.append("Pin appears on multiple nets: " + str(key))
            pin_nets[key] = net["name"]
    design = {"components": [{**p, "pads": [{"ref": p["ref"], "number": pin, "net": name}
               for (ref, pin), name in pin_nets.items() if ref == p["ref"]]} for p in parts],
              "nets": {net["name"]: net for net in nets}}
    contract = copy.deepcopy(spec.get("pcb_constraints", {"schema_version": VERSION,
        "design_revision": spec["revision"], "upstream": {"status": spec["status"], "open_issues": list(spec.get("open_issues", []))},
        "footprints": {}, "groups": [], "pad_pairs": [], "fixed": {}, "regions": [], "keepouts": []}))
    if contract["design_revision"] != spec["revision"]:
        errors.append("A constraint revision differs from design_spec")
    # Never allow a stale embedded 'ready' status to erase current upstream issues.
    contract["upstream"] = {"status": spec["status"], "open_issues": list(spec.get("open_issues", [])),
                            "source_sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest()}
    if spec.get("formal_handoff") is False:
        contract["upstream"]["open_issues"].append("A_DRAFT_NOT_FORMAL")
    evidence = json.loads(Path(footprint_evidence).read_text(encoding="utf-8-sig")) if footprint_evidence else {}
    if not isinstance(evidence, dict):
        raise ValueError("Footprint evidence must be an object keyed by reference")
    contract["footprints"] = {p["ref"]: evidence.get(p["ref"], {"status": "candidate", "footprint": p["footprint"], "evidence": []}) for p in parts}
    for ref in set(evidence) - set(refs):
        errors.append("Evidence names unknown component " + ref)
    for p in parts:
        if contract["footprints"][p["ref"]].get("footprint") != p["footprint"]:
            errors.append("Footprint evidence differs from B: " + p["ref"])
    binding_data = json.loads(Path(bindings).read_text(encoding="utf-8-sig")) if bindings else {"decoupling": []}
    if set(binding_data) - {"decoupling"} or not isinstance(binding_data.get("decoupling"), list):
        raise ValueError("bindings must contain decoupling array")
    for binding in binding_data["decoupling"]:
        allowed = {"id", "ic", "capacitor", "supply_pin", "ground_pin", "capacitor_supply_pin", "capacitor_ground_pin", "max_distance_mm", "evidence"}
        if not isinstance(binding, dict) or set(binding) - allowed:
            raise ValueError("Unknown decoupling binding fields")
        for polarity in ("supply", "ground"):
            first = binding["ic"], binding[polarity + "_pin"]
            second = binding["capacitor"], binding["capacitor_" + polarity + "_pin"]
            if first not in pin_nets or second not in pin_nets or pin_nets[first] != pin_nets[second]:
                errors.append("Decoupling binding crosses nets or names missing pins: " + binding["id"])
            contract.setdefault("pad_pairs", []).append({"id": binding["id"] + ":" + polarity,
                "a": list(first), "b": list(second), "kind": "decoupling", "weight": 8,
                "max_distance_mm": binding.get("max_distance_mm"), "evidence": binding.get("evidence", [])})
    try:
        validate_constraints(contract, design)
    except ValueError as error:
        errors.append(str(error))
    output.mkdir(parents=True, exist_ok=True)
    if errors:
        report = {"status": "blocked", "errors": errors, "component_count": len(parts), "ready_for_prototype": False}
        (output / "preflight.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    with tempfile.TemporaryDirectory(prefix=".B-to-C-", dir=output.parent) as temp:
        stage = Path(temp)
        erc_status = "not_assessed"
        erc_details = None
        if schematic:
            schematic = Path(schematic).resolve(strict=True)
            _copy_schematic(schematic, stage, {})
            for suffix in (".kicad_pro", ".kicad_dru"):
                file = schematic.with_suffix(suffix)
                if file.is_file():
                    shutil.copyfile(file, stage / file.name)
            table = schematic.parent / "fp-lib-table"
            if table.is_file():
                shutil.copyfile(table, stage / "fp-lib-table")
                for lib in _items(_parse(table.read_text(encoding="utf-8-sig")), "lib"):
                    uri = next((x[1] for x in _items(lib, "uri")), "")
                    if str(uri).startswith("${KIPRJMOD}/"):
                        folder = (schematic.parent / str(uri).removeprefix("${KIPRJMOD}/")).resolve(strict=True)
                        if not folder.is_relative_to(schematic.parent):
                            raise ValueError("Footprint library leaves project")
                        target = stage / folder.relative_to(schematic.parent)
                        if folder.is_dir():
                            shutil.copytree(folder, target)
                        else:
                            raise ValueError("Footprint library must be a directory")
            cli = find_kicad_cli(cli_path)
            if cli is None:
                raise RuntimeError("Native ERC/export requires KiCad CLI")
            exported = stage / "input.xml"
            subprocess.run([str(cli), "sch", "export", "netlist", "--format", "kicadxml", "--output", str(exported), str(stage / schematic.name)], check=True, capture_output=True, timeout=60)
            if _identity(*_read_netlist(exported)) != _identity(parts, nets):
                raise ValueError("B netlist is stale or differs from native schematic")
            erc = subprocess.run([str(cli), "sch", "erc", "--format", "json", "--severity-all", "--exit-code-violations",
                                  "--output", str(stage / "ERC.json"), str(stage / schematic.name)], capture_output=True, timeout=60)
            raw = json.loads((stage / "ERC.json").read_text(encoding="utf-8-sig")) if (stage / "ERC.json").is_file() else {}
            count = erc_violation_count(raw)
            erc_status = "passed" if erc.returncode == 0 and count == 0 else "failed"
            erc_details = {
                "returncode": erc.returncode, "violations": count,
                "kicad_version": raw.get("kicad_version") if isinstance(raw, dict) else None,
                "ignored_checks": raw.get("ignored_checks", []) if isinstance(raw, dict) else [],
            }
        else:
            shutil.copyfile(netlist, stage / ("input.xml" if netlist.suffix == ".xml" else "input.net"))
        if erc_status != "passed":
            contract["upstream"]["open_issues"].append("B_NATIVE_ERC_" + erc_status.upper())
        entry = {"schema_version": "B-to-C-1", "design_revision": spec["revision"],
                 "netlist": "input.xml" if schematic or netlist.suffix == ".xml" else "input.net",
                 "schematic": schematic.name if schematic else None, "constraints": "pcb_constraints.json",
                 "scope": "draft_only", "upstream_status": spec["status"], "ERC": erc_status}
        (stage / "project_input.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (stage / "pcb_constraints.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report = {"status": "draft_input_prepared", "errors": [], "component_count": len(parts), "ERC": erc_status,
                  "erc_details": erc_details,
                  "ready_for_prototype": False, "note": "C must measure footprints, route, verify and apply the prototype release gate"}
        (stage / "preflight.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for file in stage.iterdir():
            target = output / file.name
            shutil.copytree(file, target) if file.is_dir() else shutil.copyfile(file, target)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("design_spec", type=Path)
    parser.add_argument("netlist", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--schematic", type=Path)
    parser.add_argument("--footprint-evidence", type=Path)
    parser.add_argument("--bindings", type=Path)
    parser.add_argument("--kicad-cli", type=Path)
    args = parser.parse_args()
    try:
        result = prepare(args.design_spec, args.netlist, args.output, schematic=args.schematic,
                         footprint_evidence=args.footprint_evidence, bindings=args.bindings, cli_path=args.kicad_cli)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        print("B -> C preparation failed: " + str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
