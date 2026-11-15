"""Schematic, placement, routing and KiCad DRC entry point."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp

from kicad_io import load_design, write_board
from model_strategy import model_order, validate_strategy
from placement import board_size, boost_profile, place, placed_pads
from router import route_board
from verify import find_kicad_cli, render_preview, run_drc


ROOT = Path(__file__).resolve().parent
DEMO = ROOT / "demo" / "Boost练习.kicad_sch"
DEMO_STRATEGY = ROOT / "demo" / "布局策略.json"


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _route_length(route):
    return sum(math.dist(item["start"], item["end"]) for item in route["segments"])


def _result_rank(drc, route):
    if drc["status"] == "failed":
        return (1, len(route["unrouted"]), math.inf, math.inf)
    return (0, drc["unconnected"], drc["errors"], drc["warnings"])


def _copy_rules(source, destination):
    for suffix in (".kicad_pro", ".kicad_dru"):
        file = source.with_suffix(suffix)
        if file.is_file():
            shutil.copyfile(file, destination / file.name)


def _apply_strategy(profile, proposed):
    for index, name in enumerate(proposed["ordered_nets"]):
        extra = 0.60 / (index + 1)
        profile["weights"][name] = profile["weights"].get(name, 1.0) + extra
    profile["pairs"].extend(proposed["layout_pairs"])


def generate(
    schematic,
    output_parent,
    *,
    cli_path=None,
    footprint_libraries=(),
    footprint_source=None,
    dimensions=None,
    signal_width=0.25,
    power_width=0.45,
    clearance=0.20,
    attempts=4,
    use_model=False,
    strategy_file=None,
    progress=print,
):
    source = Path(schematic).resolve()
    if not source.is_file() or source.suffix.lower() not in (".kicad_sch", ".sch", ".net", ".xml"):
        raise ValueError("请选择可编辑的 KiCad 原理图或 KiCad 网表文件。")
    if not all(math.isfinite(v) and 0.15 <= v <= 5 for v in (signal_width, power_width, clearance)):
        raise ValueError("线宽和间距应在 0.15–5 mm 范围内。")
    if not isinstance(attempts, int) or not 1 <= attempts <= 8:
        raise ValueError("尝试次数应为 1–8。")
    if dimensions is not None and (
        len(dimensions) != 2 or not all(math.isfinite(v) and 8 <= v <= 100 for v in dimensions)
    ):
        raise ValueError("演示算法的板宽和板高应在 8–100 mm 范围内。")
    cli = find_kicad_cli(cli_path)
    if cli is None:
        raise RuntimeError("找不到 KiCad。请选择 KiCad 安装目录的 bin/kicad-cli.exe。")
    output_parent = Path(output_parent).resolve()
    output_parent.mkdir(parents=True, exist_ok=True)
    run_dir = Path(mkdtemp(prefix=datetime.now().strftime("PCB-%Y%m%d-%H%M%S-"), dir=output_parent))
    candidates_dir = run_dir / "candidates"
    candidates_dir.mkdir()
    modern = source.suffix.lower() == ".kicad_sch"
    progress("正在从原理图取得网络与封装…")
    design = load_design(
        source, kicad_cli=cli, footprint_roots=footprint_libraries,
        footprint_source=Path(footprint_source).resolve() if footprint_source else None,
        work_dir=candidates_dir,
    )
    if not design["components"] or not design["nets"]:
        raise ValueError("输入中没有可生成的元件或网络。")
    if design.get("hierarchical"):
        raise ValueError("当前版本接收单页平面原理图。多页工程可先导出完整 KiCad 网表再生成 PCB。")
    if modern and not design.get("schematic_paths_linked"):
        raise ValueError("无法把封装关联到原理图中的位号。请在 KiCad 重新保存原理图并核对位号。")
    if len(design["components"]) > 30 or sum(len(c["pads"]) for c in design["components"]) > 150:
        raise ValueError("当前版本最多处理 30 个元件与 150 个焊盘。")
    width, height = dimensions or board_size(design["components"])
    if max(width, height) > 100:
        raise ValueError("推算的板尺寸超过 100 mm。请缩小设计范围。")
    profile = boost_profile(design)
    strategy = {"mode": "local", "notes": "按 Boost 规则和网络连接选取布局、布线顺序。"}
    model_priority = []
    if strategy_file:
        if use_model:
            raise ValueError("请选择一种策略来源。")
        preset = Path(strategy_file)
        proposed = validate_strategy(json.loads(preset.read_text(encoding="utf-8-sig")), design)
        model_priority = proposed["ordered_nets"]
        _apply_strategy(profile, proposed)
        strategy = {"mode": "strategy_file", "notes": f"读取策略文件 {preset.name}。", **proposed}
    elif use_model:
        progress("正在请 Codex 读取网络，提出布局及布线优先级…")
        try:
            proposed = model_order(design, profile, run_dir)
        except (OSError, ValueError, RuntimeError) as error:
            strategy = {"mode": "fallback", "notes": str(error)}
            progress("模型暂不可用，继续执行本地自动设计。")
        else:
            model_priority = proposed["ordered_nets"]
            _apply_strategy(profile, proposed)
            strategy = {"mode": "codex", "notes": "模型提出优先级，本地算法计算坐标和走线。", **proposed}
    roles = profile["roles"]
    power_names = {roles[r] for r in ("VIN", "VBUS", "VOUT", "GND", "PGND", "SW", "LX") if r in roles}
    power_names.update(n for n in design["nets"] if n.strip("/").upper() in {"GND", "PGND"})
    widths = {name: (power_width if name in power_names else signal_width) for name in design["nets"]}
    counts = Counter(p["net"] for c in design["components"] for p in c["pads"] if p["net"])
    chosen = list(dict.fromkeys([
        *model_priority,
        *(roles[r] for r in ("SW", "LX", "FB", "VOUT", "VIN") if r in roles),
        *sorted(counts, key=lambda n: (-profile["weights"].get(n, 1), counts[n], n)),
    ]))
    chosen = [net for net in chosen if counts[net] > 1]
    if not chosen:
        raise ValueError("没有拥有两个或更多焊盘的可布线网络。")

    evidence = []
    best = None
    for number in range(attempts):
        progress(f"正在尝试布局和两层布线（{number + 1}/{attempts}）…")
        try:
            placement = place(design, width, height, profile, seed=number)
        except ValueError as error:
            evidence.append({"candidate": number + 1, "placement_error": str(error)})
            continue
        order = chosen if number % 2 == 0 else sorted(chosen, key=lambda net: (counts[net], chosen.index(net)))
        route = route_board(
            placed_pads(design, placement), width, height, widths=widths,
            clearance=clearance, grid=0.10, edge_margin=0.60, order=order, timeout=19,
        )
        candidate_dir = candidates_dir / f"方案{number + 1}"
        candidate_dir.mkdir()
        _copy_rules(source, candidate_dir)
        if modern:
            shutil.copyfile(source, candidate_dir / source.name)
        tentative = candidate_dir / f"{source.stem}.kicad_pcb"
        write_board(tentative, design, placement, width, height, route["segments"], route["vias"])
        drc = run_drc(tentative, tentative.with_suffix(".json"), cli, schematic_parity=modern)
        rank = (*_result_rank(drc, route), len(route["unrouted"]), _route_length(route))
        evidence.append({
            "candidate": number + 1, "router_unrouted": route["unrouted"],
            "drc_unconnected": drc["unconnected"], "drc_errors": drc["errors"],
            "drc_warnings": drc["warnings"], "drc_status": drc["status"],
        })
        if best is None or rank < best[0]:
            best = rank, placement, route, tentative
        if not route["unrouted"] and drc["status"] == "passed":
            break
    if best is None:
        _write_json(run_dir / "步骤.json", evidence)
        raise RuntimeError(f"无法在 {width:g} × {height:g} mm 内完成初始布局。请扩大板框。")

    _, placement, route, selected = best
    board = run_dir / f"{source.stem}.kicad_pcb"
    shutil.copyfile(selected, board)
    _copy_rules(source, run_dir)
    schematic_copy = None
    if modern:
        schematic_copy = run_dir / source.name
        shutil.copyfile(source, schematic_copy)
    progress("正在对选出的 PCB 执行 KiCad DRC…")
    drc = run_drc(board, run_dir / "DRC.json", cli, schematic_parity=modern)
    preview = run_dir / "PCB顶层.png"
    if not render_preview(board, preview, cli):
        preview = run_dir / "PCB铜层.svg"
        if not render_preview(board, preview, cli):
            preview = None
    _write_json(run_dir / "布局与布线.json", {
        "source": str(source), "board_dimensions_mm": [width, height],
        "component_count": len(design["components"]), "routable_net_count": len(chosen),
        "strategy": strategy, "detected_roles": roles, "widths_mm": widths,
        "route_clearance_mm": clearance, "placement": placement, "route": route,
        "attempts": evidence, "drc": drc,
    })
    complete = (
        not route["unrouted"] and drc["status"] != "failed"
        and drc["errors"] == drc["unconnected"] == drc["schematic_parity"] == 0
    )
    report = run_dir / "设计报告.md"
    lines = [
        "# KiCad 自动 PCB 设计", "",
        f"- 输入文件：{source.name}",
        f"- 生成板文件：{board.name}",
        f"- 尺寸：{width:g} × {height:g} mm，两层铜",
        f"- 设计规模：{len(placement)} 个器件，{len(chosen)} 个待布线网络",
        f"- 自动生成：{len(route['segments'])} 段铜线、{len(route['vias'])} 个过孔",
        f"- 布线器未完成的网络：{'、'.join(route['unrouted']) or '无'}",
        f"- 策略来源：{strategy['mode']}。{strategy['notes']}",
        f"- 初版状态：{'已布通，DRC 无错误' if complete else '待处理检查结果'}", "",
        "## KiCad DRC", "",
    ]
    if drc["status"] == "failed":
        lines.append(f"KiCad 检查未完成。{drc.get('error', '')}")
    else:
        lines.extend((
            f"KiCad {drc['kicad_version']} 报告 {drc['errors']} 个错误、{drc['warnings']} 条警告，"
            f"未连接 {drc['unconnected']} 项。具体位置见 DRC.json。", "",
            f"原理图一致性：{'发现 ' + str(drc['schematic_parity']) + ' 项差异。' if modern else '本次使用网表，未执行。'}", "",
            "| 检查类型 | 数量 |", "| --- | ---: |",
            *(f"| `{name}` | {amount} |" for name, amount in drc["types"].items()),
        ))
    lines.extend((
        "", "## 布局依据和核对", "",
        "根据网表和关键网络优先级布置器件，并检查铜间距和焊盘连通。"
        "板级 DRC 覆盖本次 KiCad 加载的检查规则。线宽的载流能力、功率回流、"
        "反馈耦合和器件极性需结合实际硬件约束复核。", "",
        "实物的纹波、温升和效率，需对实际工程验证。", "",
    ))
    report.write_text("\n".join(lines), encoding="utf-8")
    return {"directory": run_dir, "board": board, "drc": drc, "preview": preview,
            "report": report, "complete": complete, "schematic": schematic_copy}


def main():
    parser = argparse.ArgumentParser(description="从 KiCad 原理图自动生成两层 PCB。")
    parser.add_argument("schematic", nargs="?", type=Path)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "results")
    parser.add_argument("--kicad-cli", type=Path)
    parser.add_argument("--footprints", type=Path, action="append", default=[])
    parser.add_argument("--footprint-source", type=Path, help="复用此 PCB 中的封装几何，用于旧工程。")
    parser.add_argument("--size", type=float, metavar=("WIDTH", "HEIGHT"), nargs=2)
    parser.add_argument("--signal-width", type=float, default=0.25)
    parser.add_argument("--power-width", type=float, default=0.45)
    parser.add_argument("--clearance", type=float, default=0.20)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--model", action="store_true", help="请已登录的 Codex 建议优先级，发送网络和器件数据。")
    parser.add_argument("--strategy", type=Path, help="使用指定的 JSON 布局策略文件。")
    args = parser.parse_args()
    if args.demo == bool(args.schematic):
        parser.error("请提供原理图文件，或指定 --demo。")
    try:
        result = generate(
            DEMO if args.demo else args.schematic, args.output, cli_path=args.kicad_cli,
            footprint_libraries=args.footprints, footprint_source=args.footprint_source,
            dimensions=args.size, signal_width=args.signal_width, power_width=args.power_width,
            clearance=args.clearance, attempts=args.attempts, use_model=args.model,
            strategy_file=args.strategy,
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"生成失败：{error}", file=sys.stderr)
        return 1
    print(f"PCB：{result['board']}\n报告：{result['report']}")
    print("初版已布通，DRC 无错误。" if result["complete"] else "请查看报告中的未完成项。")
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
