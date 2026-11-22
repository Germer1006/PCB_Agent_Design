"""Render declared single-sheet connectivity to native KiCad and prepare B→C."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4

from prepare_pcb_input import _identity, _read_netlist, prepare
from kicad_io import Quoted as Q, _field, _items, _parse, emit
from verify import find_kicad_cli


def uid():
    return Q(str(uuid4()))


def at(point, angle=0):
    return ["at", *point, angle]


def text_effects(size, *, hide=False):
    return ["effects", ["font", ["size", size, size]], *([["hide", "yes"]] if hide else [])]


def library_symbols(root, library):
    tree = _parse((root / (library + ".kicad_sym")).read_text(encoding="utf-8-sig"))
    return {symbol[1]: symbol for symbol in _items(tree, "symbol")}


def load_symbol(root, lib_id, libraries):
    library, _, name = lib_id.partition(":")
    if not re.fullmatch(r"[A-Za-z0-9_+.-]+", library) or not name:
        raise ValueError(f"Expected a library:part symbol ID: {lib_id}")
    if library not in libraries:
        libraries[library] = library_symbols(root, library)
    record = libraries[library].get(name)
    if record is None or _items(record, "extends"):
        raise ValueError(f"Symbol missing or uses inheritance: {lib_id}")
    pins = {}
    for unit in _items(record, "symbol"):
        unit_id, style = unit[1].rsplit("_", 2)[-2:]
        if unit_id not in {"0", "1"} or style not in {"0", "1"}:
            raise ValueError(f"Multi-unit or alternate-style symbol needs a supplied schematic: {lib_id}")
        for pin in _items(unit, "pin"):
            number = _field(pin, "number")
            _, x, y, angle = _items(pin, "at")[0]
            pose = (float(x), float(y), int(angle))
            if not number or number in pins or pose[2] not in {0, 90, 180, 270}:
                raise ValueError(f"Duplicate or unsupported pin on {lib_id}")
            pins[number] = pose
    if not pins or len({p[:2] for p in pins.values()}) != len(pins):
        raise ValueError(f"Symbol has no eligible pins or has overlapping pin anchors: {lib_id}")
    symbol = deepcopy(record)
    symbol[1] = Q(lib_id)
    return symbol, pins


def render(intent, stem, symbol_root):
    if not isinstance(intent, dict) or set(intent) - {"schema_version", "components", "nets", "no_connect"}:
        raise ValueError("Expected schematic-intent-1")
    if intent.get("schema_version") != "schematic-intent-1":
        raise ValueError("Unsupported schematic intent version")
    components = intent["components"]
    if not isinstance(components, list) or not 1 <= len(components) <= 30:
        raise ValueError("Single-sheet generator supports 1–30 declared symbols")
    root_id = uid()
    symbols, expected_parts, anchors, libraries = {}, {}, {}, {}
    schematic_parts = []
    for index, part in enumerate(components):
        if not isinstance(part, dict) or set(part) != {"ref", "lib_id", "value", "footprint"} or not all(
                isinstance(v, str) and v.strip() for v in part.values()):
            raise ValueError("Each component needs ref, lib_id, value and footprint")
        ref, lib_id, value, footprint = (part[k] for k in ("ref", "lib_id", "value", "footprint"))
        if not re.fullmatch(r"[A-Z]+[1-9][0-9]*", ref) or ref in expected_parts or ":" not in footprint:
            raise ValueError(f"Invalid or duplicate reference/footprint at {ref}")
        if lib_id not in symbols:
            symbols[lib_id] = load_symbol(symbol_root, lib_id, libraries)
        _, pins = symbols[lib_id]
        if any(abs(x) > 18 or abs(y) > 15 for x, y, _ in pins.values()):
            raise ValueError(f"Symbol {lib_id} exceeds the single-sheet grid cell")
        x, y = 38.10 + (index % 5) * 76.2, 25.40 + (index // 5) * 43.18
        properties = [
            ["property", Q("Reference"), Q(ref), at((round(x - 3.81, 4), round(y - 2.54, 4))),
             text_effects(1.27)],
            ["property", Q("Value"), Q(value), at((round(x + 3.81, 4), round(y + 2.54, 4))),
             text_effects(1.0)],
            ["property", Q("Footprint"), Q(footprint), at((x, y)), text_effects(1.0, hide=True)],
        ]
        for pin, (dx, dy, angle) in pins.items():
            anchors[ref, pin] = (round(x + dx, 4), round(y - dy, 4), angle)
        schematic_parts.append(["symbol", ["lib_id", Q(lib_id)], at((round(x, 4), round(y, 4))), ["unit", 1],
                                ["in_bom", "yes"], ["on_board", "yes"], ["dnp", "no"], ["uuid", uid()],
                                *properties, *[["pin", Q(pin), ["uuid", uid()]] for pin in pins],
                                ["instances", ["project", Q(stem), ["path", Q("/" + root_id),
                                 ["reference", Q(ref)], ["unit", 1]]]]])
        expected_parts[ref] = (value, footprint)

    expected_pins, net_names, wiring = {}, set(), []

    def endpoint(value):
        if not isinstance(value, list) or len(value) != 2 or not all(isinstance(x, str) for x in value):
            raise ValueError("Net endpoints must be [reference, pin number]")
        key = tuple(value)
        if key not in anchors or key in expected_pins:
            raise ValueError(f"Pin missing or declared more than once: {key}")
        return key

    if not isinstance(intent["nets"], list) or not isinstance(intent.get("no_connect", []), list):
        raise ValueError("nets and no_connect must be arrays")
    for net in intent["nets"]:
        if not isinstance(net, dict) or set(net) != {"name", "pins"}:
            raise ValueError("Each net must contain only name and pins")
        pins = net["pins"]
        name = net["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.+-]{0,23}", name) or name in net_names:
            raise ValueError(f"Invalid or duplicate net label: {name}")
        if not isinstance(pins, list) or len(pins) < 2:
            raise ValueError(f"Net {name} needs at least two pins; declare intentionally unused pins separately")
        net_names.add(name)
        for item in pins:
            key = endpoint(item)
            expected_pins[key] = "/" + name
            x, y, angle = anchors[key]
            dx, dy = {0: (-3.81, 0), 90: (0, 3.81), 180: (3.81, 0), 270: (0, -3.81)}[angle]
            end = (round(x + dx, 4), round(y + dy, 4))
            wiring.extend([["wire", ["pts", ["xy", x, y], ["xy", *end]],
                            ["stroke", ["width", 0], ["type", "default"]], ["uuid", uid()]],
                           ["label", Q(name), at(end, 90 if dy else 0),
                            ["effects", ["font", ["size", .9, .9]], ["justify", "left", "bottom"]], ["uuid", uid()]]])
    for item in intent.get("no_connect", []):
        key = endpoint(item)
        expected_pins[key] = None
        wiring.append(["no_connect", ["at", *anchors[key][:2]], ["uuid", uid()]])
    if missing := anchors.keys() - expected_pins.keys():
        raise ValueError(f"Declare every pin as connected or no_connect: {sorted(missing)}")
    tree = ["kicad_sch", ["version", 20250114], ["generator", Q("eeschema")], ["generator_version", Q("9.0")],
            ["uuid", root_id], ["paper", Q("A3")], ["lib_symbols", *[item[0] for item in symbols.values()]],
            *wiring, *schematic_parts, ["sheet_instances", ["path", Q("/"), ["page", Q("1")]]]]
    text = "\n".join(["(kicad_sch", *[emit(item) for item in tree[1:]], ")"])
    used_libraries = {}
    for lib_id in sorted(symbols):
        library, name = lib_id.split(":", 1)
        used_libraries.setdefault(library, []).append(libraries[library][name])
    return text, expected_parts, expected_pins, used_libraries


def freeze_symbol_libraries(work, libraries):
    directory = work / "symbols"
    directory.mkdir()
    table = ["sym_lib_table", ["version", 7]]
    for library, symbols in libraries.items():
        filename = library + ".kicad_sym"
        contents = ["kicad_symbol_lib", ["version", 20231120], ["generator", Q("kicad_symbol_editor")], *symbols]
        (directory / filename).write_text(emit(contents) + "\n", encoding="utf-8")
        table.append(["lib", ["name", Q(library)], ["type", Q("KiCad")],
                      ["uri", Q("${KIPRJMOD}/symbols/" + filename)], ["options", Q("")], ["descr", Q("")]])
    (work / "sym-lib-table").write_text(emit(table) + "\n", encoding="utf-8")


def generate(spec_path, output, cli, symbol_root):
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    schematic, expected_parts, expected_pins, used_libraries = render(spec.get("schematic_intent"), "circuit", symbol_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".native-schematic-", dir=output.parent) as work:
        work = Path(work)
        source, netlist = work / "circuit.kicad_sch", work / "circuit.xml"
        source.write_text(schematic + "\n", encoding="utf-8")
        source.with_suffix(".kicad_pro").write_text("{}\n", encoding="utf-8")
        freeze_symbol_libraries(work, used_libraries)
        subprocess.run([str(cli), "sch", "export", "netlist", "--format", "kicadxml",
                        "--output", str(netlist), str(source)], check=True, capture_output=True, timeout=60)
        parts, pins = _identity(*_read_netlist(netlist))
        no_connect = {pin for pin, net in expected_pins.items() if net is None}
        actual_connected = {pin: net for pin, net in pins.items() if pin not in no_connect}
        if (parts != expected_parts or actual_connected != {pin: net for pin, net in expected_pins.items() if net}
                or any(pin in pins and not pins[pin].startswith("unconnected-(") for pin in no_connect)):
            raise ValueError("Native schematic connectivity differs from design intent")
        return prepare(spec_path, netlist, output, schematic=source, cli_path=cli)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("design_spec", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kicad-cli", type=Path)
    parser.add_argument("--symbol-root", type=Path, help="Directory containing the KiCad .kicad_sym libraries")
    args = parser.parse_args()
    cli = find_kicad_cli(args.kicad_cli)
    if cli is None:
        parser.exit(2, "KiCad CLI is required\n")
    root = args.symbol_root or cli.parent.parent / "share" / "kicad" / "symbols"
    try:
        result = generate(args.design_spec, args.output, cli, root)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(2, f"Schematic generation blocked: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
