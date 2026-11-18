"""Portable project copying, explicit rules and private footprint namespaces."""
from __future__ import annotations

import copy
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from kicad_io import Quoted, _parse, _items, emit, native_operation

CRITICAL_CHECKS = ("clearance", "shorting_items", "tracks_crossing", "unconnected_items", "invalid_outline",
                   "track_width", "hole_clearance", "hole_to_hole", "drill_out_of_range")


def find_schematic(source, explicit=None):
    source = Path(source).resolve()
    if explicit:
        candidate = Path(explicit).resolve(strict=True)
        if candidate.suffix != ".kicad_sch":
            raise ValueError("source_schematic must be a modern .kicad_sch")
        return candidate
    if source.suffix == ".kicad_sch":
        return source
    if source.suffix == ".xml":
        root = ET.parse(source).getroot()
        filename = root.findtext("design/source", "").replace("\\", "/").rsplit("/", 1)[-1]
        candidate = source.parent / filename
        if filename and candidate.suffix == ".kicad_sch" and candidate.is_file():
            return candidate.resolve()
    return None


def _copy_schematic(source, destination, mapping):
    base = source.parent.resolve()
    visited = set()
    def visit(path):
        path = path.resolve(strict=True)
        if not path.is_relative_to(base):
            raise ValueError("Hierarchical sheet leaves project root; make it self-contained first")
        if path in visited:
            return
        visited.add(path)
        tree = _parse(path.read_text(encoding="utf-8-sig"))
        for symbol in _items(tree, "symbol"):
            for prop in _items(symbol, "property"):
                if prop[1] == "Footprint" and prop[2] in mapping:
                    prop[2] = Quoted(mapping[prop[2]])
        for sheet in _items(tree, "sheet"):
            for prop in _items(sheet, "property"):
                if prop[1] == "Sheetfile":
                    name = str(prop[2]).replace("${KIPRJMOD}", str(base))
                    if "$" in name:
                        raise ValueError("Unresolved sheet path " + name)
                    visit(path.parent / name)
        output = destination / path.relative_to(base)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(emit(tree) + "\n", encoding="utf-8")
    visit(source)
    for name in ("sym-lib-table",):
        table = base / name
        if table.is_file():
            shutil.copyfile(table, destination / name)
            for lib in _items(_parse(table.read_text(encoding="utf-8-sig")), "lib"):
                uri = next((x[1] for x in _items(lib, "uri")), "")
                if str(uri).startswith("${KIPRJMOD}/"):
                    file = (base / str(uri).removeprefix("${KIPRJMOD}/")).resolve(strict=True)
                    if not file.is_relative_to(base) or not file.is_file():
                        raise ValueError("Invalid project symbol library")
                    target = destination / file.relative_to(base)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(file, target)


def configure_project(path, source_project, signal_width, power_width, clearance):
    """Preserve source rules; refuse narrower requested geometry than source minima."""
    source = json.loads(source_project.read_text(encoding="utf-8-sig")) if source_project and source_project.is_file() else {}
    project = copy.deepcopy(source)
    settings = project.setdefault("board", {}).setdefault("design_settings", {})
    rules = settings.setdefault("rules", {})
    for key, requested in (("min_track_width", min(signal_width, power_width)), ("min_clearance", clearance)):
        existing = rules.get(key, 0)
        if isinstance(existing, bool) or not isinstance(existing, (float, int)):
            raise ValueError("Invalid source board rule " + key)
        if existing > requested + 1e-9:
            raise ValueError(f"Requested geometry {requested}mm is below source {key}={existing}mm; do not relax rules to pass")
    defaults = {"min_clearance": clearance, "min_track_width": min(signal_width, power_width),
                "min_via_diameter": .6, "min_through_hole_diameter": .3, "min_via_annular_width": .1,
                "min_hole_clearance": .25, "min_hole_to_hole": .25, "min_copper_edge_clearance": .25,
                "min_silk_clearance": .1, "min_text_height": .8, "min_text_thickness": .1,
                "max_error": .005, "min_resolved_spokes": 2}
    for key, value in defaults.items():
        rules.setdefault(key, value)
    # An inherited zero means no board minimum; netclass defaults alone do not
    # enforce the requested floor for other source netclasses.
    rules["min_clearance"] = clearance
    rules["min_track_width"] = min(signal_width, power_width)
    settings.setdefault("meta", {"filename": "board_design_settings.json", "version": 2})
    if settings.get("drc_exclusions"):
        raise ValueError("Source contains DRC exclusions; remove/review them instead of silently inheriting them")
    settings["drc_exclusions"] = []
    severity = settings.setdefault("rule_severities", {})
    for key in CRITICAL_CHECKS:
        if key in severity and severity[key] != "error":
            raise ValueError("Critical DRC check must remain error: " + key)
        severity[key] = "error"
    net_settings = project.setdefault("net_settings", {})
    classes = net_settings.setdefault("classes", [])
    default = next((item for item in classes if item.get("name") == "Default"), None)
    if default and default.get("clearance", 0) > clearance + 1e-9:
        raise ValueError("Requested clearance is below existing Default netclass clearance")
    if default is None:
        default = {"name": "Default", "priority": 2147483647}
        classes.append(default)
    default.update(clearance=clearance, track_width=signal_width, via_diameter=.65, via_drill=.3,
                   microvia_diameter=.3, microvia_drill=.1, diff_pair_width=signal_width,
                   diff_pair_gap=clearance, diff_pair_via_gap=clearance, bus_width=12, wire_width=6,
                   line_style=0, pcb_color="rgba(0, 0, 0, 0.000)", schematic_color="rgba(0, 0, 0, 0.000)")
    net_settings.setdefault("meta", {"version": 4})
    net_settings.setdefault("netclass_patterns", [])
    project.setdefault("meta", {}).update(filename=path.name, version=1)
    path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"source_project_supplied": bool(source), "rules_mm": rules,
            "ignored_checks": sorted(k for k, v in severity.items() if v == "ignore"),
            "manufacturing_process_confirmed": False}


def prepare_project(source, design, destination, *, cli, signal_width, power_width, clearance,
                    source_schematic=None, constraints=None):
    """Copy all native inputs and freeze exactly the footprints measured by C."""
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    design = copy.deepcopy(design)
    schematic = find_schematic(source, source_schematic)
    stem = schematic.stem if schematic else Path(source).stem
    mapping = native_operation(design, "freeze", {"components": design["components"],
                               "template": design["_template_path"],
                               "directory": str(destination / "PCB_Project.pretty")}, destination)
    for part in design["components"]:
        old = part["footprint"]
        part["original_footprint"] = old
        part["footprint"] = mapping[old]
        part["_library"] = str(destination / "PCB_Project.pretty")
        part["_module"] = mapping[old].split(":", 1)[1]
    (destination / "fp-lib-table").write_text(
        '(fp_lib_table (version 7) (lib (name "PCB_Project") (type "KiCad") '
        '(uri "${KIPRJMOD}/PCB_Project.pretty") (options "") (descr "Frozen project footprints")))\n', encoding="utf-8")
    if schematic:
        _copy_schematic(schematic, destination, mapping)
    netlist_text = Path(design["_netlist_path"]).read_text(encoding="utf-8-sig")
    if netlist_text.lstrip().startswith("<"):
        root = ET.fromstring(netlist_text)
        for comp in root.findall("./components/comp"):
            footprint = comp.find("footprint")
            if footprint is not None and footprint.text in mapping:
                footprint.text = mapping[footprint.text]
        ET.ElementTree(root).write(destination / "input.net.xml", encoding="utf-8", xml_declaration=True)
    else:
        tree = _parse(netlist_text)
        for components in _items(tree, "components"):
            for comp in _items(components, "comp"):
                for fp in _items(comp, "footprint"):
                    if fp[1] in mapping:
                        fp[1] = Quoted(mapping[fp[1]])
        (destination / "input.net").write_text(emit(tree) + "\n", encoding="utf-8")
    if constraints is not None:
        (destination / "pcb_constraints.json").write_text(json.dumps(constraints, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    origin = schematic or Path(source)
    source_project = origin.with_suffix(".kicad_pro")
    rule_info = configure_project(destination / (stem + ".kicad_pro"), source_project,
                                  signal_width, power_width, clearance)
    custom = origin.with_suffix(".kicad_dru")
    if custom.is_file():
        shutil.copyfile(custom, destination / (stem + ".kicad_dru"))
    return design, stem, schematic is not None, {"footprint_aliases": mapping, "rules": rule_info}
