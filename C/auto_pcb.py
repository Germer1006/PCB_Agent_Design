"""Native KiCad PCB drafts, constrained placement and independent release checks."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
import shutil
import sys
from tempfile import mkdtemp

from constraints import attach_profile, check_layout, read_constraints
from critical_routes import check_routes, plan_pairs
from delivery import package_delivery
from kicad_io import load_design, native_operation, write_board
from model_strategy import model_order, validate_strategy
from placement import board_size, boost_profile, place, placed_pads
from project_files import prepare_project
from release_check import check_readback, evaluate, read_expected, seal_verification
from router import _Grid, route_board
from verify import find_kicad_cli, render_preview, run_drc

ROOT = Path(__file__).resolve().parent
DEMO = ROOT / "demo" / "Boost练习.kicad_sch"
DEMO_STRATEGY = ROOT / "demo" / "布局策略.json"


def _write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _route_length(route):
    return sum(math.dist(item["start"], item["end"]) for item in route["segments"])


def _result_rank(drc, route, constraint_failures, copper):
    if drc["status"] == "failed":
        return True, constraint_failures, len(route["unrouted"]), _route_length(route)
    return (False, constraint_failures, drc["unconnected"], drc["errors"], drc["warnings"],
            sum(c["status"] == "not_assessed" for c in copper),
            sum(c["layer_changes"] or 0 for c in copper),
            sum(c["track_length_mm"] or 0 for c in copper), _route_length(route))


def _apply_strategy(profile, proposed):
    for index, name in enumerate(proposed["ordered_nets"]):
        profile["weights"][name] = profile["weights"].get(name, 1.0) + .60 / (index + 1)
    profile["pairs"].extend(proposed["layout_pairs"])


def _input_paths(source, constraints_file, source_schematic):
    source = Path(source).resolve(strict=True)
    if source.suffix != ".json":
        return source, constraints_file, source_schematic
    entry = json.loads(source.read_text(encoding="utf-8-sig"))
    if entry.get("schema_version") != "B-to-C-1":
        raise ValueError("Expected B-to-C-1 project_input.json")
    def resolve(name):
        relative = entry.get(name)
        if not relative:
            return None
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError("B-to-C paths must be relative")
        path = (source.parent / relative).resolve(strict=True)
        if not path.is_relative_to(source.parent):
            raise ValueError("B-to-C path leaves project directory")
        return path
    netlist, contract, schematic = resolve("netlist"), resolve("constraints"), resolve("schematic")
    if netlist is None or contract is None:
        raise ValueError("B-to-C input must include netlist and constraints")
    if read_constraints(contract)["design_revision"] != entry.get("design_revision"):
        raise ValueError("B-to-C revision differs from constraints")
    return netlist, constraints_file or contract, source_schematic or schematic


def generate(schematic, output_parent, *, cli_path=None, footprint_libraries=(), footprint_source=None,
             dimensions=None, signal_width=.25, power_width=.45, clearance=.20, attempts=4,
             use_model=False, strategy_file=None, constraints_file=None, source_schematic=None,
             max_components=30, max_pads=150, grid=.10, max_grid_cells=650_000, router_timeout=19,
             freerouting_jar=None, ground_planes=False, review_file=None, preview=True, progress=print):
    source, constraints_file, source_schematic = _input_paths(schematic, constraints_file, source_schematic)
    if source.suffix.lower() not in {".kicad_sch", ".sch", ".net", ".xml"}:
        raise ValueError("Select a native schematic/netlist or B project_input.json")
    if not all(not isinstance(v, bool) and math.isfinite(v) and .15 <= v <= 5 for v in (signal_width, power_width, clearance)):
        raise ValueError("线宽和间距应在 0.15–5 mm 范围内。")
    if power_width < signal_width:
        raise ValueError("Power width must not be below signal width")
    for name, value, upper in (("attempts", attempts, 8), ("max_components", max_components, 200), ("max_pads", max_pads, 1500)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
            raise ValueError(f"{name} must be an integer from 1 to {upper}")
    if isinstance(grid, bool) or not math.isfinite(grid) or not .05 <= grid <= .5:
        raise ValueError("Grid must be 0.05–0.5mm")
    if isinstance(router_timeout, bool) or not math.isfinite(router_timeout) or not 1 <= router_timeout <= 300:
        raise ValueError("Router timeout must be 1–300 seconds")
    if dimensions is not None and (len(dimensions) != 2 or not all(
            not isinstance(v, bool) and math.isfinite(v) and 8 <= v <= 100 for v in dimensions)):
        raise ValueError("Board dimensions must be 8–100mm")
    if use_model and strategy_file:
        raise ValueError("Choose one strategy source")
    cli = find_kicad_cli(cli_path)
    if cli is None:
        raise RuntimeError("KiCad CLI not found")
    output_parent = Path(output_parent).resolve()
    output_parent.mkdir(parents=True, exist_ok=True)
    run_dir = Path(mkdtemp(prefix=datetime.now().strftime("PCB-%Y%m%d-%H%M%S-"), dir=output_parent))
    progress("Reading full native netlist and measuring footprints…")
    design = load_design(source, kicad_cli=cli, footprint_roots=footprint_libraries,
                         footprint_source=Path(footprint_source).resolve() if footprint_source else None,
                         work_dir=run_dir / "input_metrics")
    if not design["components"] or not design["nets"]:
        raise ValueError("Empty design")
    if source.suffix == ".kicad_sch" and not design["schematic_paths_linked"]:
        raise ValueError("Cannot bind footprints to native schematic paths")
    count, pad_count = len(design["components"]), sum(len(c["pads"]) for c in design["components"])
    if count > max_components or pad_count > max_pads:
        raise ValueError(f"Design has {count} components/{pad_count} pads; budget is {max_components}/{max_pads}. "
                         "Raise --max-components/--max-pads explicitly; never drop parts.")
    width, height = dimensions or board_size(design["components"])
    if max(width, height) > 100:
        raise ValueError("Estimated board exceeds 100mm; supply explicit dimensions")
    _Grid(width, height, grid, max_grid_cells)
    constraints = read_constraints(constraints_file, design)
    profile = attach_profile(boost_profile(design), constraints)
    strategy, priority = {"mode": "local", "notes": "Native solver with supplied placement constraints"}, []
    if strategy_file:
        proposed = validate_strategy(json.loads(Path(strategy_file).read_text(encoding="utf-8-sig")), design)
        _apply_strategy(profile, proposed)
        priority = proposed["ordered_nets"]
        strategy = {"mode": "strategy_file", "notes": str(strategy_file)}
    elif use_model:
        try:
            proposed = model_order(design, profile, run_dir)
            _apply_strategy(profile, proposed)
            priority = proposed["ordered_nets"]
            strategy = {"mode": "codex", "notes": "Model suggests order; deterministic validators execute it"}
        except (OSError, ValueError, RuntimeError) as error:
            strategy = {"mode": "fallback", "notes": str(error)}
    progress("Freezing project libraries, rules and all schematic pages…")
    design, stem, parity, info = prepare_project(source, design, run_dir / "input_project", cli=cli,
        signal_width=signal_width, power_width=power_width, clearance=clearance,
        source_schematic=source_schematic, constraints=constraints)
    origins = {part["ref"]: part["original_footprint"] for part in design["components"]}
    _write_json(run_dir / "project_provenance.json", info)
    roles = profile["roles"]
    power = {roles[r] for r in ("VIN", "VBUS", "VOUT", "GND", "PGND", "SW", "LX") if r in roles}
    power.update(n for n in design["nets"] if n.strip("/").rsplit("/", 1)[-1].upper() in {"GND", "PGND", "VBOOST", "VIN_RAW", "VIN_F"})
    widths = {n: power_width if n in power else signal_width for n in design["nets"]}
    counts = Counter(p["net"] for c in design["components"] for p in c["pads"] if p["net"])
    role_priority = {"+3V3": 0, "3V3": 0, "VDD": 0, "VDDA": 0, "VREF+": 0,
                     "PWM_SW": 1, "SWDIO": 1, "SWCLK": 1, "NRST": 1, "BOOT0": 1}
    ground = roles.get("GND")
    if ground_planes and not ground:
        raise ValueError("Cannot add ground planes without an unambiguous GND net")
    fill_candidates = ground_planes and not freerouting_jar

    def add_planes(board, work_dir):
        native_operation(design, "planes", {"board": str(board), "ground": ground,
            "dimensions": [width, height], "clearance": clearance}, work_dir)

    hard_priority = [n for n in counts if n.rsplit("/", 1)[-1].upper() in role_priority]
    hard_priority.sort(key=lambda n: (role_priority[n.rsplit("/", 1)[-1].upper()], n))
    order = list(dict.fromkeys([*hard_priority, *priority,
        *(roles[r] for r in ("SW", "LX", "FB", "VOUT", "VIN") if r in roles),
        *sorted(counts, key=lambda n: (-profile["weights"].get(n, 1), counts[n], n))]))
    if ground_planes and ground in order:
        order.remove(ground)
        order.append(ground)
    order = [n for n in order if counts[n] > 1]
    if not order:
        raise ValueError("No multi-pad nets")
    priority_pairs = plan_pairs((p for c in design["components"] for p in c["pads"]), constraints)
    evidence, best = [], None
    for number in range(attempts):
        progress(f"Placement/routing candidate {number + 1}/{attempts}…")
        candidate = run_dir / "candidates" / str(number + 1)
        try:
            placement = place(design, width, height, profile, seed=number)
            layout = check_layout(design, placement, constraints)
            geometry_failures = [c["id"] for c in layout["checks"] if c["status"] == "failed"]
            route = route_board(placed_pads(design, placement), width, height, widths=widths,
                clearance=clearance, grid=grid, edge_margin=.60,
                order=order if number % 2 == 0 else sorted(order, key=lambda n: (
                    n == ground and ground_planes, n not in hard_priority, counts[n], order.index(n))),
                priority_pairs=priority_pairs, timeout=router_timeout, max_cells=max_grid_cells,
                ground_escape_width=signal_width)
        except ValueError as error:
            evidence.append({"candidate": number + 1, "error": str(error)})
            continue
        shutil.copytree(run_dir / "input_project", candidate)
        board = candidate / (stem + ".kicad_pcb")
        write_board(board, design, placement, width, height, route["segments"], route["vias"])
        if fill_candidates:
            add_planes(board, candidate)
        drc = run_drc(board, candidate / "DRC.json", cli, schematic_parity=parity)
        copper = check_routes(board, constraints)
        copper_failures = [c["id"] for c in copper if c["status"] == "failed"]
        copper_unknown = sum(c["status"] == "not_assessed" for c in copper)
        failures = len(geometry_failures) + len(copper_failures)
        rank = _result_rank(drc, route, failures, copper)
        evidence.append({"candidate": number + 1, "layout_failures": len(geometry_failures),
                         "failed_geometry": geometry_failures, "failed_critical_routes": copper_failures,
                         "unassessed_critical_routes": copper_unknown, "priority_pairs": route["priority_pairs"],
                         "router_unrouted": route["unrouted"], "pad_failures": route["failures"],
                         "fanout": route["fanout"], "drc": drc})
        if best is None or rank < best[0]:
            best = rank, placement, route, candidate
        if not failures and not copper_unknown and drc["status"] == "passed":
            break
    _write_json(run_dir / "candidate_results.json", evidence)
    if best is None:
        raise RuntimeError("No feasible placement; see " + str(run_dir / "candidate_results.json"))
    _, placement, route, chosen = best
    project = run_dir / "project"
    shutil.copytree(chosen, project, ignore=shutil.ignore_patterns("DRC.json"))
    board = project / (stem + ".kicad_pcb")
    repair = {"engine": "local", "notes": "No external repair used"}
    if freerouting_jar:
        initial = run_drc(board, run_dir / "before_repair_DRC.json", cli, schematic_parity=parity)
        if initial["status"] != "failed" and initial["unconnected"]:
            from freerouting_adapter import reroute
            progress("Repairing control/GND routing with explicit Freerouting adapter…")
            initial_copper = check_routes(board, constraints)
            repaired, repair = reroute(board, design, run_dir / "repair", jar=freerouting_jar,
                signal_width=signal_width, power_width=power_width, clearance=clearance, power_nets=power)
            backup = run_dir / "before_repair.kicad_pcb"
            shutil.copyfile(board, backup)
            shutil.copyfile(repaired, board)
            repaired_drc = run_drc(board, run_dir / "after_repair_DRC.json", cli, schematic_parity=parity)
            def repair_rank(result):
                return (result["status"] == "failed", result.get("unconnected") if result.get("unconnected") is not None else math.inf,
                        result.get("errors") if result.get("errors") is not None else math.inf)
            repaired_copper = check_routes(board, constraints)
            severity = {"passed": 0, "not_assessed": 1, "failed": 2}
            before = {c["id"]: c["status"] for c in initial_copper}
            after = {c["id"]: c["status"] for c in repaired_copper}
            regressed = sorted(identity for identity, status in before.items()
                               if severity[after.get(identity, "failed")] > severity[status])
            if regressed or repair_rank(repaired_drc) > repair_rank(initial):
                shutil.copyfile(backup, board)
                repair["critical_route_regressions"] = regressed
                repair["status"] = "rejected_critical_route_regression" if regressed else "rejected_worse_DRC"
            else:
                repair["status"] = "accepted_by_DRC_pending_power_review"
    if ground_planes and not fill_candidates:
        add_planes(board, run_dir)
    progress("Checking native PCB, source pin assignments and power-layout constraints…")
    drc = run_drc(board, run_dir / "DRC.json", cli, schematic_parity=parity, refill_zones=True)
    audit = native_operation(design, "audit", {"board": str(board)}, run_dir)
    placement = {p["ref"]: {"x": p["x"], "y": p["y"], "angle": int(round(p["angle"])) % 360} for p in audit["components"]}
    layout = check_layout(design, placement, constraints)
    copper = check_routes(board, constraints)
    layout["checks"].extend(copper)
    if copper:
        layout["scope"] = "Placement geometry and identifiable copper paths. Electrical return and thermal design require review."
    if any(c["status"] == "failed" for c in copper):
        layout["status"] = "failed"
    elif layout["status"] == "passed" and any(c["status"] == "not_assessed" for c in copper):
        layout["status"] = "needs_review"
    width_failures = sorted({s["net"] for s in audit["route"]["segments"]
        if s["net"] in power and s["net"].strip("/").rsplit("/", 1)[-1].upper() not in {"GND", "PGND"}
        and s["width"] + 1e-6 < power_width})
    for net in width_failures:
        layout["checks"].append({"id": "power_width:" + net, "status": "failed",
                                 "detail": "Routed power copper narrower than configured power width"})
    if width_failures:
        layout["status"] = "failed"
    readback = check_readback(*read_expected(project), audit)
    _write_json(run_dir / "layout_check.json", layout)
    _write_json(run_dir / "readback.json", readback)
    seal_verification(run_dir, project, board, drc, audit, layout, origins)
    if review_file:
        shutil.copyfile(review_file, run_dir / "release_review.json")
    gate = evaluate(run_dir, require_roundtrip=False)
    _write_json(run_dir / "release_status.json", gate)
    routing_complete = drc["status"] != "failed" and drc["unconnected"] == 0 and readback["status"] == "passed"
    complete = routing_complete and drc["status"] == "passed" and layout["status"] != "failed"
    state = "pcb_verified_review_required" if complete else "needs_rework"
    _write_json(run_dir / "布局与布线.json", {"source": str(source), "board_dimensions_mm": [width, height],
        "component_count": count, "physical_pad_count": pad_count, "routable_net_count": len(order),
        "strategy": strategy, "repair": repair, "placement": placement, "route": audit["route"],
        "router_diagnostics": {"unrouted": route["unrouted"], "priority_pairs": route["priority_pairs"],
                               "pad_failures": route["failures"], "fanout": route["fanout"]}, "layout": layout, "drc": drc,
        "prototype_ready": gate["prototype_ready"]})
    _write_json(run_dir / "execution_status.json", {"status": state, "routing_complete": routing_complete,
        "prototype_ready": gate["prototype_ready"], "production_ready": False, "components": count,
        "physical_pads": pad_count, "explicit_capacity_budget": [max_components, max_pads],
        "repair": repair, "drc": drc, "layout_status": layout["status"], "readback": readback})
    preview_path = None
    if preview:
        (run_dir / "preview").mkdir()
        for filename in ("PCB顶层.png", "PCB铜层.svg"):
            candidate_preview = run_dir / "preview" / filename
            if render_preview(board, candidate_preview, cli):
                preview_path = candidate_preview
                break
    critical_rows = []
    for finding in copper:
        metrics = ""
        if finding["track_length_mm"] is not None:
            metrics = f"，可识别铜线长度 {finding['track_length_mm']:g} mm、换层 {finding['layer_changes']} 次"
        critical_rows.append(f"- `{finding['id']}` {finding['status']}{metrics}。{finding['detail']}")
    report = run_dir / "设计报告.md"
    report.write_text("\n".join(["# PCB 草稿与交付检查", "", f"- 状态：{state}",
        f"- 工程：project/{stem}.kicad_pro", f"- 规模：{count} 器件 / {pad_count} 焊盘；{width:g} × {height:g} mm，两层",
        f"- 原生读回：{readback['status']}", f"- DRC：{drc['status']}；错误 {drc['errors']}，警告 {drc['warnings']}，未连接 {drc['unconnected']}",
        f"- 原理图一致性：{'已执行' if parity else '网表输入，仅执行引脚网络读回，未执行原生原理图一致性'}",
        f"- 布局与关键走线：{layout['status']}；回流和热设计仍需审核", f"- 布线来源：{repair['engine']}",
        f"- 信号线宽/间距目标：{signal_width:g}/{clearance:g} mm，完整规则见项目 .kicad_pro",
        "- DRC 通过后仍须工程审核；量产审核未实现。", "", "## 关键走线检查", "",
        *critical_rows, "", "## 当前放行阻塞项", "",
        *[f"- `{b['id']}`：{b['detail']}" for b in gate["blockers"]], "", "## 交付复查", "",
        "完整解压 delivery.zip，保留 project 中的规则、子页及 PCB_Project.pretty。",
        "运行 `python C/release_check.py <解压目录>` 查看放行状态。未知限值及候选封装不能当作通过。",
        "程序执行原生 CLI 与 ZIP 重开检查；GUI 重开须在目标 KiCad 中执行并记录。", ""]), encoding="utf-8")
    progress("Extracting delivery ZIP and repeating native DRC/readback…")
    roundtrip = package_delivery(run_dir, cli=cli, design=design)
    gate = evaluate(run_dir)
    return {"directory": run_dir, "board": board, "drc": drc, "preview": preview_path, "report": report,
            "complete": complete, "routing_complete": routing_complete, "prototype_ready": gate["prototype_ready"],
            "release": gate, "roundtrip": roundtrip, "delivery": run_dir / "delivery.zip",
            "schematic": project / (stem + ".kicad_sch") if parity else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schematic", nargs="?", type=Path)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "results")
    parser.add_argument("--kicad-cli", type=Path)
    parser.add_argument("--footprints", type=Path, action="append", default=[])
    parser.add_argument("--footprint-source", type=Path)
    parser.add_argument("--source-schematic", type=Path)
    parser.add_argument("--size", type=float, nargs=2)
    parser.add_argument("--signal-width", type=float, default=.25)
    parser.add_argument("--power-width", type=float, default=.45)
    parser.add_argument("--clearance", type=float, default=.2)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--max-components", type=int, default=30)
    parser.add_argument("--max-pads", type=int, default=150)
    parser.add_argument("--grid", type=float, default=.1)
    parser.add_argument("--max-grid-cells", type=int, default=650_000)
    parser.add_argument("--router-timeout", type=float, default=19)
    parser.add_argument("--constraints", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--model", action="store_true", help="Explicitly send net/part summary to configured Codex")
    parser.add_argument("--strategy", type=Path)
    parser.add_argument("--freerouting-jar", type=Path, help="Explicit official Freerouting 2.x JAR")
    parser.add_argument("--ground-planes", action="store_true")
    parser.add_argument("--no-preview", action="store_true")
    parser.add_argument("--target", choices=("draft", "prototype"), default="draft")
    args = parser.parse_args()
    if args.demo == bool(args.schematic):
        parser.error("Provide a schematic/input manifest, or --demo")
    try:
        result = generate(DEMO if args.demo else args.schematic, args.output, cli_path=args.kicad_cli,
            footprint_libraries=args.footprints, footprint_source=args.footprint_source,
            source_schematic=args.source_schematic, dimensions=args.size, signal_width=args.signal_width,
            power_width=args.power_width, clearance=args.clearance, attempts=args.attempts, use_model=args.model,
            strategy_file=args.strategy, constraints_file=args.constraints, review_file=args.review,
            max_components=args.max_components, max_pads=args.max_pads, grid=args.grid,
            max_grid_cells=args.max_grid_cells, router_timeout=args.router_timeout,
            freerouting_jar=args.freerouting_jar, ground_planes=args.ground_planes, preview=not args.no_preview)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"生成失败：{error}", file=sys.stderr)
        return 1
    print(f"PCB: {result['board']}\nReport: {result['report']}\nZIP: {result['delivery']}")
    print(f"Routing complete: {result['routing_complete']}; prototype ready: {result['prototype_ready']}")
    return 0 if (result["prototype_ready"] if args.target == "prototype" else result["complete"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
