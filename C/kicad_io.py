"""Read KiCad's schematic netlist and create a fresh KiCad board."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


_SEXPR = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class Quoted(str):
    """Preserve quoted strings when copying native hierarchical schematics."""


def emit(tree):
    if isinstance(tree, list):
        return "(" + " ".join(emit(item) for item in tree) + ")"
    return json.dumps(str(tree), ensure_ascii=False) if isinstance(tree, Quoted) else str(tree)


def _parse(text):
    root = []
    stack = [root]
    for match in _SEXPR.finditer(text):
        token = match.group()
        if token == "(":
            nested = []
            stack[-1].append(nested)
            stack.append(nested)
        elif token == ")":
            if len(stack) == 1:
                raise ValueError("KiCad 文件中的括号不匹配。")
            stack.pop()
        else:
            if token.startswith('"'):
                token = Quoted(json.loads(token))
            stack[-1].append(token)
    if len(stack) != 1 or len(root) != 1 or not isinstance(root[0], list):
        raise ValueError("KiCad 文件中的括号不匹配。")
    return root[0]


def _items(tree, tag):
    return [item for item in tree if isinstance(item, list) and item and item[0] == tag]


def _field(tree, tag, default=""):
    return next((item[1] for item in _items(tree, tag) if len(item) > 1), default)


def _sexpr_netlist(text):
    root = _parse(text)
    if not root or root[0] != "export":
        raise ValueError("输入不是 KiCad 网表。")
    parts = []
    nets = []
    for group in _items(root, "components"):
        for component in _items(group, "comp"):
            sheets = _items(component, "sheetpath")
            properties = {
                _field(field, "name"): _field(field, "value", "yes")
                for field in _items(component, "property")
            }
            parts.append({
                "ref": _field(component, "ref"), "value": _field(component, "value"),
                "footprint": _field(component, "footprint"),
                "sheet": _field(sheets[0], "tstamps", "/") if sheets else "/",
                "symbols": _field(component, "tstamps", _field(component, "tstamp")),
                "properties": properties,
            })
    for group in _items(root, "nets"):
        for net in _items(group, "net"):
            nets.append({
                "name": _field(net, "name"),
                "nodes": [{"ref": _field(node, "ref"), "pin": _field(node, "pin")}
                          for node in _items(net, "node")],
            })
    return parts, nets


def _xml_netlist(text):
    root = ET.fromstring(text)
    if root.tag != "export":
        raise ValueError("输入不是 KiCad 网表。")
    parts = [{
        "ref": element.get("ref", ""), "value": element.findtext("value", ""),
        "footprint": element.findtext("footprint", ""),
        "sheet": element.find("sheetpath").get("tstamps", "/") if element.find("sheetpath") is not None else "/",
        "symbols": element.findtext("tstamps") or element.findtext("tstamp", ""),
        "properties": {prop.get("name"): prop.get("value", "yes") for prop in element.findall("property")},
    } for element in root.findall("./components/comp")]
    nets = [{
        "name": element.get("name", ""),
        "nodes": [{"ref": node.get("ref", ""), "pin": node.get("pin", "")} for node in element.findall("node")],
    } for element in root.findall("./nets/net")]
    return parts, nets


def _enabled_flag(value):
    return value is not None and value.lower() not in ("no", "false", "0")


def _instance_path(sheet, symbols):
    parts = sheet.strip("/").split("/") if sheet.strip("/") else []
    if not symbols.strip():
        return ""
    # Older netlists use unsigned 32-bit timestamps. KiCad embeds these in UUIDs.
    parts.append(symbols.split()[0])
    try:
        return "/" + "/".join(str(uuid.UUID(part.zfill(32))) for part in parts)
    except ValueError:
        return ""


def _invoke(argv, timeout):
    try:
        result = subprocess.run(
            [str(arg) for arg in argv], capture_output=True, timeout=timeout, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{Path(argv[0]).name} 运行超时。") from error
    if result.returncode:
        message = (result.stderr + b"\n" + result.stdout).decode("utf-8", errors="replace").strip()
        # Windows reports native exception exit codes as unsigned values.
        raise RuntimeError(f"{Path(argv[0]).name} 执行失败（{result.returncode}）。{message[-1800:]}")


def _find_python(cli):
    candidates = [cli.parent / "python.exe", cli.parent / "python3"]
    if os.name == "nt":
        candidates.extend(cli.parent.glob("python3*.exe"))
    for executable in candidates:
        if executable.is_file():
            return executable
    return Path(sys.executable)


def _library_map(directory, builtin):
    path = directory / "fp-lib-table"
    if not path.is_file():
        return {}
    variables = dict(os.environ)
    variables.update(KIPRJMOD=str(directory))
    for version in range(8, 13):
        variables.setdefault(f"KICAD{version}_FOOTPRINT_DIR", str(builtin))
    result = {}
    for library in _items(_parse(path.read_text(encoding="utf-8-sig")), "lib"):
        location = _field(library, "uri")
        if _field(library, "type").lower() != "kicad" or not location:
            continue
        expanded = _VARIABLE.sub(lambda match: variables.get(match[1], match[0]), location)
        candidate = Path(expanded)
        if "${" in expanded or "://" in expanded:
            continue
        if not candidate.is_absolute():
            candidate = directory / candidate
        result[_field(library, "name")] = candidate
    return result


def _find_footprint(identifier, local_libraries, roots):
    library, separator, name = identifier.partition(":")
    if not separator:
        name, library = library, ""
    if not name or any(part in (".", "..") or re.search(r"[\\/:]", part) for part in (name, library)):
        raise ValueError(f"封装标识无效 {identifier}。")
    candidates = []
    if library in local_libraries:
        candidates.append(local_libraries[library])
    for root in roots:
        if library:
            candidates.append(root / f"{library}.pretty")
            if root.name == f"{library}.pretty":
                candidates.append(root)
        else:
            candidates.append(root)
    return next((str(folder.resolve()) for folder in candidates if (folder / f"{name}.kicad_mod").is_file()), None), name


def load_design(schematic: Path, *, kicad_cli: Path, footprint_roots=(),
                footprint_source: Path | None = None, work_dir: Path) -> dict:
    """Read the selected schematic or explicit KiCad netlist and measure each footprint."""
    schematic = Path(schematic).resolve(strict=True)
    cli = Path(kicad_cli).resolve(strict=True)
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    suffix = schematic.suffix.lower()
    if suffix in {".kicad_sch", ".sch"}:
        netlist = work_dir / "schematic-netlist.xml"
        _invoke([cli, "sch", "export", "netlist", "--format", "kicadxml", "--output", netlist, schematic], 35)
    elif suffix in {".net", ".xml"}:
        netlist = schematic
    else:
        raise ValueError("请选择可编辑的 KiCad 原理图（.kicad_sch）或 KiCad 网表（.net）。")

    text = netlist.read_text(encoding="utf-8-sig")
    parts, nodes = _xml_netlist(text) if text.lstrip().startswith("<") else _sexpr_netlist(text)
    parts = [part for part in parts if not part["ref"].startswith("#")
             and not _enabled_flag(part["properties"].get("exclude_from_board"))]
    if not parts:
        raise ValueError("没有导出可放置的器件。旧版 .sch 请先在 KiCad 中转换为 .kicad_sch，或选择原厂 .net。")
    refs = {part["ref"] for part in parts}
    if "" in refs or len(refs) != len(parts):
        raise ValueError("原理图包含空白或重复位号，请先编号。")
    unassigned = [part["ref"] for part in parts if not part["footprint"]]
    if unassigned:
        raise ValueError("原理图缺少封装，请先在 KiCad 中分配：" + "、".join(unassigned[:20]))
    nets = {}
    pad_nets = {}
    for net in nodes:
        name = net["name"]
        relevant = [node for node in net["nodes"] if node["ref"] in refs]
        if not relevant:
            continue
        if not name or name in nets:
            raise ValueError("网表含有空白或重复网络名。")
        nets[name] = {"code": len(nets) + 1, "name": name, "nodes": relevant}
        for node in relevant:
            pin = (node["ref"], node["pin"])
            if pin in pad_nets and pad_nets[pin] != name:
                raise ValueError(f"{pin[0]}.{pin[1]} 同时出现在不同网络，请先修复原理图。")
            pad_nets[pin] = name
    if not any(len(net["nodes"]) > 1 for net in nets.values()):
        raise ValueError("网表没有可布线的器件连接，请核对原理图。")

    builtin = cli.parent.parent / "share" / "kicad" / "footprints"
    local_libraries = _library_map(schematic.parent, builtin)
    roots = [*(Path(root).expanduser() for root in footprint_roots), builtin]
    geometry_board = None
    if footprint_source is not None:
        source = Path(footprint_source).resolve(strict=True)
        if source.suffix.lower() != ".kicad_pcb":
            raise ValueError("参考封装板应为 .kicad_pcb。")
        geometry_board = work_dir / "embedded-footprints.kicad_pcb"
        shutil.copyfile(source, geometry_board)
        _invoke([cli, "pcb", "upgrade", geometry_board], 30)
    for part in parts:
        library, name = _find_footprint(part["footprint"], local_libraries, roots)
        if not library and not geometry_board:
            raise ValueError(f"{part['ref']} 找不到封装 {part['footprint']}。请提供项目封装库。")
        part["_library"], part["_module"] = library, name
        part["_path"] = _instance_path(part["sheet"], part["symbols"])
        part["_pad_nets"] = {pin: name for (ref, pin), name in pad_nets.items() if ref == part["ref"]}

    python = _find_python(cli)
    request = work_dir / "load-request.json"
    response = work_dir / "footprint-metrics.json"
    request.write_text(json.dumps({
        "parts": parts, "geometry_board": str(geometry_board) if geometry_board else None,
    }, ensure_ascii=False), encoding="utf-8")
    _invoke([python, "-B", "-X", "utf8", __file__, "measure", request, response], 35)
    measured = json.loads(response.read_text(encoding="utf-8"))
    return {
        "components": measured, "nets": nets, "source_type": suffix.lstrip("."),
        "hierarchical": any(part["sheet"].strip("/") for part in parts),
        "schematic_paths_linked": suffix == ".kicad_sch" and all(part["_path"] for part in parts),
        "_template_path": str(geometry_board) if geometry_board else None, "_python": str(python),
        "_netlist_path": str(netlist), "_source_path": str(schematic),
    }


def native_operation(design, operation, request, work_dir, timeout=60):
    """Run pcbnew in KiCad's own Python; do not require it in the caller's Python."""
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix=".native-", dir=work_dir) as directory:
        directory = Path(directory)
        source, result = directory / "request.json", directory / "response.json"
        source.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        _invoke([design["_python"], "-B", "-X", "utf8", __file__, operation, source, result], timeout)
        return json.loads(result.read_text(encoding="utf-8"))


def write_board(path, design, placement, board_width, board_height, segments, vias):
    """Write generated geometry without carrying over copper from a reference board."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    form = path.with_name(f".{path.stem}-{uuid.uuid4().hex[:10]}-write.json")
    working = form.with_suffix(".kicad_pcb")
    try:
        form.write_text(json.dumps({
            "design": design, "placement": placement, "board_width": board_width,
            "board_height": board_height, "segments": segments, "vias": vias,
        }, ensure_ascii=False), encoding="utf-8")
        _invoke([design["_python"], "-B", "-X", "utf8", __file__, "write", form, working], 35)
        working.replace(path)
    finally:
        working.unlink(missing_ok=True)
        form.unlink(missing_ok=True)
        # Native SaveBoard may create a project next to the temporary board.
        # Remove only sidecars belonging to this call's fresh UUID filename.
        for suffix in (".kicad_pro", ".kicad_prl", ".lck"):
            working.with_suffix(suffix).unlink(missing_ok=True)


def _native_library_plugin(pcb):
    manager = pcb.PCB_IO_MGR
    lookup = getattr(manager, "FindPlugin", None) or manager.PluginFind
    return lookup(manager.KICAD_SEXP)


def _load_footprint(pcb, part, reference_board):
    if part["_library"]:
        footprint = _native_library_plugin(pcb).FootprintLoad(part["_library"], part["_module"])
    else:
        compatible = [footprint for footprint in reference_board.GetFootprints()
                      if footprint.GetFPIDAsString() == part["footprint"]]
        original = next((item for item in compatible if item.GetReference() == part["ref"]), None)
        if original is None and compatible:
            original = compatible[0]
        footprint = pcb.FOOTPRINT(original) if original is not None else None
    if footprint is None:
        raise ValueError(f"{part['ref']} 找不到封装 {part['footprint']}。")
    if footprint.IsFlipped():
        footprint.Flip(footprint.GetPosition(), pcb.FLIP_DIRECTION_LEFT_RIGHT)
    footprint.SetOrientationDegrees(0)
    footprint.SetPosition(pcb.VECTOR2I(0, 0))
    return footprint


def _pad_layers(pcb, pad):
    if pad.GetAttribute() == pcb.PAD_ATTRIB_NPTH:
        return [0, 1]
    return [index for index, side in enumerate((pcb.F_Cu, pcb.B_Cu)) if pad.IsOnLayer(side)]


def _measure(pcb, request):
    reference = pcb.LoadBoard(request["geometry_board"]) if request.get("geometry_board") else None
    names = {
        pcb.PAD_SHAPE_CIRCLE: "circle", pcb.PAD_SHAPE_RECT: "rect",
        pcb.PAD_SHAPE_OVAL: "oval", pcb.PAD_SHAPE_ROUNDRECT: "roundrect",
    }
    for part in request["parts"]:
        footprint = _load_footprint(pcb, part, reference)
        footprint.SetReference(part["ref"])
        footprint.SetValue(part["value"])
        bbox = footprint.GetBoundingBox(False, False)
        pads = []
        for pad in footprint.Pads():
            layers = _pad_layers(pcb, pad)
            if not layers:
                continue
            anchor = pad.GetPosition()
            bounds = pad.GetBoundingBox()
            is_hole = pad.GetAttribute() == pcb.PAD_ATTRIB_NPTH
            width = 2 * max(abs(anchor.x - bounds.GetLeft()), abs(bounds.GetRight() - anchor.x))
            height = 2 * max(abs(anchor.y - bounds.GetTop()), abs(bounds.GetBottom() - anchor.y))
            drill = pad.GetDrillSize()
            if not width or not height:
                continue
            if pad.GetOffset().x or pad.GetOffset().y or pad.GetOrientationDegrees() % 90:
                shape = "rect"
            else:
                shape = names.get(pad.GetShape(), "rect")
            number = pad.GetNumber()
            pads.append({
                "ref": part["ref"], "number": number, "net": "" if is_hole else part["_pad_nets"].get(number, ""),
                "x": pcb.ToMM(anchor.x), "y": pcb.ToMM(anchor.y),
                "w": pcb.ToMM(width), "h": pcb.ToMM(height), "layers": layers, "shape": shape,
                "drill": pcb.ToMM(max(drill.x, drill.y)),
            })
        absent = set(part["_pad_nets"]).difference(pad["number"] for pad in pads if pad["net"])
        if absent:
            raise ValueError(f"{part['ref']} 的封装未提供可连接焊盘 " + "、".join(sorted(absent)) + "。")
        if not pads:
            raise ValueError(f"{part['ref']} 的封装没有可用焊盘或钻孔。")
        part.update(
            width=pcb.ToMM(bbox.GetWidth()), height=pcb.ToMM(bbox.GetHeight()),
            min_x=pcb.ToMM(bbox.GetLeft()), min_y=pcb.ToMM(bbox.GetTop()),
            max_x=pcb.ToMM(bbox.GetRight()), max_y=pcb.ToMM(bbox.GetBottom()), pads=pads,
        )
    return request["parts"]


def _place_reference_fields(pcb, footprints, width, height):
    """Find readable front silkscreen positions beside the assembled parts."""
    def bounds(item, layer=None):
        box = item.GetBoundingBox() if layer is None else item.GetBoundingBox(layer)
        return tuple(pcb.ToMM(number) for number in
                     (box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom()))

    def fits(location, obstacles):
        x0, y0, x1, y1 = location
        if x0 < 0.45 or y0 < 0.45 or x1 > width - 0.45 or y1 > height - 0.45:
            return False
        gap = 0.26
        return all(x1 + gap <= left or right + gap <= x0 or y1 + gap <= top or bottom + gap <= y0
                   for left, top, right, bottom in obstacles)

    reserved = []
    for footprint in footprints:
        courtyard = [bounds(item) for item in footprint.GraphicalItems() if item.IsOnLayer(pcb.F_CrtYd)]
        if courtyard:
            reserved.append((min(r[0] for r in courtyard), min(r[1] for r in courtyard),
                             max(r[2] for r in courtyard), max(r[3] for r in courtyard)))
        reserved.extend(bounds(pad, pcb.F_Cu) for pad in footprint.Pads() if pad.IsOnLayer(pcb.F_Mask))
        reserved.extend(bounds(item) for item in footprint.GraphicalItems() if item.IsOnLayer(pcb.F_SilkS))
        reserved.extend(bounds(field) for field in footprint.GetFields()
                        if field.IsVisible() and field.IsOnLayer(pcb.F_SilkS)
                        and field.GetText() != footprint.GetReference())

    pending = []
    for footprint in sorted(footprints, key=lambda item: (len(item.GetReference()), item.GetReference())):
        reference = footprint.Reference()
        reference.SetVisible(True)
        reference.SetLayer(pcb.F_SilkS)
        reference.SetTextAngleDegrees(0)
        reference.SetTextSize(pcb.VECTOR2I(pcb.FromMM(0.85), pcb.FromMM(0.85)))
        reference.SetTextThickness(pcb.FromMM(0.14))
        original = reference.GetPosition()
        x = pcb.ToMM(original.x)
        y = pcb.ToMM(original.y)
        body = footprint.GetBoundingBox(False, False)
        x0, y0, x1, y1 = (pcb.ToMM(number) for number in
                          (body.GetLeft(), body.GetTop(), body.GetRight(), body.GetBottom()))
        center_x, center_y = ((x0 + x1) / 2, (y0 + y1) / 2)
        alternatives = []
        for distance in (0.75, 1.25, 1.75, 2.25, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 14.0):
            for offset in (0, 0.65, -0.65, 1.3, -1.3, 2.0, -2.0):
                alternatives.extend((
                    (center_x + offset, y0 - distance),
                    (center_x + offset, y1 + distance),
                    (x0 - distance - 0.3, center_y + offset),
                    (x1 + distance + 0.3, center_y + offset),
                ))
        # Keep the library location when possible. Otherwise prefer the closest clear spot.
        alternatives.sort(key=lambda point: (point[0] - x) ** 2 + (point[1] - y) ** 2)
        for point in [(x, y), *alternatives]:
            reference.SetPosition(pcb.VECTOR2I(*(pcb.FromMM(value) for value in point)))
            rectangle = bounds(reference)
            if fits(rectangle, reserved):
                reserved.append(rectangle)
                break
        else:
            reference.SetPosition(original)
            pending.append((reference, original, center_x, center_y))

    # A dense placement can exhaust the nearby positions. Look for the nearest
    # clear board space after placing the references that fit next to their parts.
    for reference, original, x, y in pending:
        old_x, old_y = pcb.ToMM(original.x), pcb.ToMM(original.y)
        left, top, right, bottom = bounds(reference)
        offsets = (left - old_x, top - old_y, right - old_x, bottom - old_y)
        positions = sorted(((i / 2, j / 2)
                            for i in range(1, int(2 * width)) for j in range(1, int(2 * height))),
                           key=lambda point: (point[0] - x) ** 2 + (point[1] - y) ** 2)
        for px, py in positions:
            rectangle = tuple(origin + offset for origin, offset in zip((px, py, px, py), offsets))
            if fits(rectangle, reserved):
                reference.SetPosition(pcb.VECTOR2I(pcb.FromMM(px), pcb.FromMM(py)))
                reserved.append(rectangle)
                break
        else:
            reserved.append(bounds(reference))


def _save_board(pcb, request, output):
    design = request["design"]
    placement = request["placement"]
    reference = pcb.LoadBoard(design["_template_path"]) if design["_template_path"] else None
    board = pcb.BOARD()
    board.SetCopperLayerCount(2)
    layers = (pcb.F_Cu, pcb.B_Cu)
    nets = {"": board.FindNet(0)}
    for name in design["nets"]:
        net = pcb.NETINFO_ITEM(board, name)
        board.Add(net)
        nets[name] = net

    def point(coords):
        return pcb.VECTOR2I(*(pcb.FromMM(float(value)) for value in coords))

    footprints = []
    for part in design["components"]:
        footprint = _load_footprint(pcb, part, reference)
        if hasattr(footprint, "SetUuid"):
            footprint.SetUuid(pcb.KIID())
        else:
            # KiCad 9 FootprintLoad allocates fresh UUIDs; m_Uuid is read-only.
            footprint.FixUuids()
        footprint.SetFPIDAsString(part["footprint"])
        footprint.SetReference(part["ref"])
        footprint.SetValue(part["value"])
        if part["_path"]:
            footprint.SetPath(pcb.KIID_PATH(part["_path"]))
        if part["properties"].get("Sheetfile"):
            footprint.SetSheetfile(part["properties"]["Sheetfile"])
        if part["properties"].get("Sheetname"):
            footprint.SetSheetname(part["properties"]["Sheetname"])
        for property_name, setter in (
            ("dnp", footprint.SetDNP), ("exclude_from_bom", footprint.SetExcludedFromBOM),
            ("exclude_from_pos_files", footprint.SetExcludedFromPosFiles),
        ):
            setter(_enabled_flag(part["properties"].get(property_name)))
        for item in [*footprint.Pads(), *footprint.GraphicalItems(), *footprint.GetFields()]:
            if hasattr(item, "SetUuid"):
                item.SetUuid(pcb.KIID())
        location = placement[part["ref"]]
        footprint.SetOrientationDegrees(location["angle"])
        footprint.SetPosition(point((location["x"], location["y"])))
        board.Add(footprint)
        footprints.append(footprint)
        for pad in footprint.Pads():
            name = "" if pad.GetAttribute() == pcb.PAD_ATTRIB_NPTH else part["_pad_nets"].get(pad.GetNumber(), "")
            pad.SetNet(nets[name])

    _place_reference_fields(pcb, footprints, request["board_width"], request["board_height"])
    outline = pcb.PCB_SHAPE(board)
    outline.SetShape(pcb.SHAPE_T_RECT)
    outline.SetLayer(pcb.Edge_Cuts)
    outline.SetStart(point((0, 0)))
    outline.SetEnd(point((request["board_width"], request["board_height"])))
    outline.SetWidth(pcb.FromMM(0.05))
    board.Add(outline)
    for segment in request["segments"]:
        start, end = point(segment["start"]), point(segment["end"])
        if start == end:
            continue
        track = pcb.PCB_TRACK(board)
        track.SetStart(start)
        track.SetEnd(end)
        track.SetWidth(pcb.FromMM(segment["width"]))
        track.SetLayer(layers[segment["layer"]])
        track.SetNet(nets[segment["net"]])
        board.Add(track)
    for spec in request["vias"]:
        via = pcb.PCB_VIA(board)
        via.SetPosition(point((spec["x"], spec["y"])))
        via.SetViaType(pcb.VIATYPE_THROUGH)
        via.SetLayerPair(*layers)
        via.SetWidth(pcb.FromMM(spec["size"]))
        via.SetDrill(pcb.FromMM(spec["drill"]))
        via.SetNet(nets[spec["net"]])
        board.Add(via)
    if not pcb.SaveBoard(str(output), board):
        raise RuntimeError("KiCad 未能保存板文件。")


def _audit_board(pcb, request):
    board = pcb.LoadBoard(request["board"])
    if board is None:
        raise ValueError("Cannot read PCB")
    if request.get("refill") and list(board.Zones()):
        if not pcb.ZONE_FILLER(board).Fill(board.Zones()):
            raise RuntimeError("Zone fill failed")
        if not pcb.SaveBoard(request["board"], board):
            raise RuntimeError("Cannot save refilled board")
    components, pads, seen, duplicates = [], [], set(), []
    for footprint in board.GetFootprints():
        components.append({"ref": footprint.GetReference(), "value": footprint.GetValue(),
                           "footprint": footprint.GetFPIDAsString(),
                           "x": pcb.ToMM(footprint.GetPosition().x),
                           "y": pcb.ToMM(footprint.GetPosition().y),
                           "angle": footprint.GetOrientationDegrees()})
        for item in [footprint, *footprint.Pads(), *footprint.GraphicalItems(), *footprint.GetFields()]:
            if hasattr(item, "m_Uuid"):
                uid = item.m_Uuid.AsString()
                if uid in seen:
                    duplicates.append(uid)
                seen.add(uid)
        for pad in footprint.Pads():
            if pad.GetAttribute() == pcb.PAD_ATTRIB_NPTH:
                continue
            pads.append({"ref": footprint.GetReference(), "number": pad.GetNumber(), "net": pad.GetNetname()})
    segments, vias = [], []
    def point(v):
        return [round(pcb.ToMM(v.x), 6), round(pcb.ToMM(v.y), 6)]
    for track in board.GetTracks():
        if isinstance(track, pcb.PCB_VIA):
            position = point(track.GetPosition())
            vias.append({"net": track.GetNetname(), "x": position[0], "y": position[1],
                         "size": pcb.ToMM(track.GetWidth(pcb.F_Cu)), "drill": pcb.ToMM(track.GetDrillValue())})
        else:
            segments.append({"net": track.GetNetname(), "layer": 0 if track.GetLayer() == pcb.F_Cu else 1,
                             "start": point(track.GetStart()), "end": point(track.GetEnd()),
                             "width": pcb.ToMM(track.GetWidth())})
    return {"components": components, "pads": pads, "copper_layers": board.GetCopperLayerCount(),
            "route": {"segments": segments, "vias": vias},
            "zones": len(list(board.Zones())), "duplicate_uuids": sorted(set(duplicates)),
            "refill_requested": bool(request.get("refill"))}


def _route_exchange(pcb, operation, request):
    board = pcb.LoadBoard(request["board"])
    if operation == "dsn":
        default = board.GetDesignSettings().m_NetSettings.GetDefaultNetclass()
        default.SetTrackWidth(pcb.FromMM(request["width"]))
        default.SetClearance(pcb.FromMM(request["clearance"]))
        default.SetViaDiameter(pcb.FromMM(.65))
        default.SetViaDrill(pcb.FromMM(.3))
        board.SynchronizeNetsAndNetClasses(False)
        if not pcb.ExportSpecctraDSN(board, request["output"]):
            raise RuntimeError("DSN export failed")
    elif operation == "ses":
        if not pcb.ImportSpecctraSES(board, request["session"]):
            raise RuntimeError("SES import failed")
        if not pcb.SaveBoard(request["output"], board):
            raise RuntimeError("Cannot save imported routing")
    elif operation == "planes":
        ground = board.FindNet(request["ground"])
        if ground is None:
            raise ValueError("Ground net not found")
        if list(board.Zones()):
            raise ValueError("Existing zones require an explicit review instead of adding overlapping planes")
        width, height = request["dimensions"]
        for layer in (pcb.F_Cu, pcb.B_Cu):
            zone = pcb.ZONE(board)
            zone.SetLayer(layer)
            zone.SetNetCode(ground.GetNetCode())
            zone.SetZoneName("Draft GND return plane; review current paths")
            zone.SetLocalClearance(pcb.FromMM(request["clearance"]))
            zone.SetMinThickness(pcb.FromMM(.2))
            zone.SetPadConnection(pcb.ZONE_CONNECTION_THT_THERMAL)
            zone.SetThermalReliefGap(pcb.FromMM(.2))
            zone.SetThermalReliefSpokeWidth(pcb.FromMM(.3))
            zone.SetIslandRemovalMode(pcb.ISLAND_REMOVAL_MODE_ALWAYS)
            zone.Outline().NewOutline()
            for x, y in ((.6, .6), (width-.6, .6), (width-.6, height-.6), (.6, height-.6)):
                zone.Outline().Append(pcb.FromMM(x), pcb.FromMM(y))
            board.Add(zone)
        if not pcb.ZONE_FILLER(board).Fill(board.Zones()) or not pcb.SaveBoard(request["board"], board):
            raise RuntimeError("Ground plane fill/save failed")
    return {"status": "completed", "operation": operation}


def _freeze_footprints(pcb, request):
    """Export exact measured libraries (including legacy embedded footprints)."""
    directory = Path(request["directory"])
    directory.mkdir(parents=True, exist_ok=True)
    reference = pcb.LoadBoard(request["template"]) if request.get("template") else None
    plugin = _native_library_plugin(pcb)
    mapping = {}
    for part in request["components"]:
        old = part["footprint"]
        if old in mapping:
            continue
        library, _, module = old.partition(":")
        name = (library + "__" + module) if module else library
        if not re.fullmatch(r"[A-Za-z0-9_,.+()\-]+", name):
            raise ValueError(f"Unsupported footprint filename {name}")
        footprint = _load_footprint(pcb, part, reference)
        footprint.SetFPIDAsString("PCB_Project:" + name)
        for pad in footprint.Pads():
            pad.SetNetCode(0)
        # KiCad 9's convenience FootprintSave cannot infer an empty .pretty
        # directory, and the native save method returns void rather than bool.
        plugin.FootprintSave(str(directory), footprint)
        if not (directory / (name + ".kicad_mod")).is_file():
            raise RuntimeError("Cannot freeze footprint " + old)
        mapping[old] = "PCB_Project:" + name
    return mapping


if __name__ == "__main__":
    import pcbnew

    operation, request_path, output_path = sys.argv[1:]
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    if operation == "measure":
        Path(output_path).write_text(json.dumps(_measure(pcbnew, request), ensure_ascii=False), encoding="utf-8")
    elif operation == "write":
        _save_board(pcbnew, request, Path(output_path))
    elif operation in ("audit", "freeze"):
        result = _audit_board(pcbnew, request) if operation == "audit" else _freeze_footprints(pcbnew, request)
        Path(output_path).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    elif operation in ("dsn", "ses", "planes"):
        result = _route_exchange(pcbnew, operation, request)
        Path(output_path).write_text(json.dumps(result), encoding="utf-8")
    else:
        raise ValueError(f"Unsupported task {operation!r}")
