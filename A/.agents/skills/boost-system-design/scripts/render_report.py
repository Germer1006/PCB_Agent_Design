"""将报告源文件中的计算占位符替换成当前设计版本的数据。"""

import argparse
import json
import re
from pathlib import Path

from _common import KNOWLEDGE_DIR, load_json, workdir_paths
from handoff_contract import b_handoff, issues, verification_plan
from design_evidence import selected


TEMPLATE = "system_design_report.template.md"
REPORT = "system_design_report.md"
PLACEHOLDER = re.compile(r"\{\{([^{}\r\n]+)\}\}")
SOURCES = {"calc": "calc", "loss": "losses", "loop": "loop", "choice": "choices", "req": "requirements"}


def render(source, paths):
    data = {}

    def substitute(match):
        kind, separator, path = match[1].strip().partition(":")
        try:
            if not separator or not path:
                raise KeyError(path)
            if kind not in data:
                data[kind] = load_json(paths[SOURCES[kind]])
            root = data[kind]
            if kind in ("calc", "loss", "loop") and "." not in path:
                step = next((step for step in root.get("steps", ()) if step["id"] == path), None)
                if step is not None:
                    return str(step["value"])
            for key in path.split("."):
                root = root[key]
            if not isinstance(root, (str, int, float, bool)):
                raise TypeError(path)
            return root if isinstance(root, str) else json.dumps(root)
        except (KeyError, TypeError) as err:
            raise ValueError(f"报告占位符 {match[0]} 无法解析到标量") from err

    result = PLACEHOLDER.sub(substitute, source)
    if "{{" in result or "}}" in result:
        raise ValueError("报告存在未解析或格式不完整的占位符")
    return result


def render_review(paths, db):
    """Build the review from the same files that drive the formal export."""
    from collections import Counter
    from export_handoff import md_table

    req, calc, sel, loss, loop, chk = (load_json(paths[key]) for key in (
        "requirements", "calc", "selection", "losses", "loop", "check"))
    sim = load_json(paths["sim"]) if paths["sim"].exists() else None
    if any(data["revision"] != chk["revision"] for data in
           (load_json(paths["choices"]), calc, sel, loss, loop, sim or chk)):
        raise ValueError("报告输入存在版本混用")
    brief = b_handoff(chk["status"])
    plan = chk["input_protection"]
    budget = chk["current_error_budget"]
    ramp = plan["model_input_ramp"]
    parts = []
    for role in sel["categories"]:
        part, count = selected(sel, db, role)
        if part:
            parts.append((role, f"[{part['mpn']}]({part['datasheet_url']})",
                          count, part.get("package"), part.get("lcsc")))
    vset, iset = calc["inputs"]["vout_v"], calc["inputs"]["iout_cc_a"]
    v_pct, i_pct = req["vout"]["steady_err_pct"] / 100, req["iout"]["steady_err_pct"] / 100
    nominal = loss["cases"]["nom"]
    sat = loss["inrush"]["inductor_isat_a"]
    lines = [
        f"版本 `{chk['revision']}`。系统设计状态 **`{chk['status']}`**。",
        "规则统计 " + " · ".join(f"{key} {value}" for key, value in chk["summary"].items()) + "。",
        "", "### B 交接范围", "",
        f"- 模式 `{brief['mode']}`。A 规格文件 `{brief['a_spec_file']}`。"
        + ("用显式草稿导出生成该文件。正式交接受当前规则阻塞。" if brief["mode"] == "exploration" else ""),
        f"- {brief['scope']} 从仓库根目录运行 `{brief['b_entrypoint']}`。"
        f"同时提供{brief['b_required_input']}，必要时补充{brief['b_optional_input']}。",
        f"- B 预检生成供 C 读取的 `{brief['c_entrypoint']}`。已有网表由 B 的原理图设计阶段提供。",
        *([f"- {brief['exploration_scope']}"] if brief["mode"] == "exploration" else []),
        "", "### 输入保护与原理图冻结条件", "",
        f"输入保护分工 `{plan['strategy']}`。实现状态 `{plan['status']}`。",
        *[f"- 待落实 {item}。" for item in plan["missing"]],
    ]
    implementation = plan["implementation"]
    for label, key in (("实施方式", "implementation"), ("外部接线条件", "connection_conditions"),
                       ("故障动作", "fault_action"), ("PWM 使能条件", "pwm_enable_condition")):
        if implementation.get(key):
            lines.append(f"- {label}。{implementation[key]}")
    if isinstance(implementation.get("current_limit_a"), dict) and not plan["missing"]:
        limits = implementation["current_limit_a"]
        lines.append(f"- 实现限流范围 {limits['min']:g}–{limits['max']:g} A。")
    if ramp:
        lines.append(f"- 受控插电仿真使用 {ramp['max_slew_v_per_ms']:g} V/ms 的输入斜率，"
                     f"源峰值预算 {ramp['source_peak_budget_a']:g} A。")
    lines.extend([
        f"- 工况包络最大输入平均电流 {plan['modeled_max_input_average_a']:.3f} A。"
        f"无缓升线性估计峰值 {loss['inrush']['i_peak_a']:g} A，电感选型采用 Isat "
        + (f"{sat:g} A。" if sat is not None else "待补证。"),
        f"- {plan['verification']}",
        "", md_table(("规则", "判定", "尚需关闭的问题", "责任模块"),
                     ((row["id"], row["verdict"], row["closure"], row["owner"]) for row in issues(chk))),
        "", "### 额定设计与验收", "",
        md_table(("项目", "数值和来源"), (
            ("额定点", f"{vset:g} V / {iset:g} A，{vset*iset:g} W"),
            ("开关频率", f"{calc['inputs']['fsw_hz']:g} Hz"),
            ("CV 验收（负载 CC）", f"{vset*(1-v_pct):g}–{vset*(1+v_pct):g} V"),
            ("CC 验收（负载 CR）", f"{iset*(1-i_pct):g}–{iset*(1+i_pct):g} A，"
                                  f"区内示例负载 {(calc['cc_range']['r_min_ohm']+calc['cc_range']['r_max_ohm'])/2:g} Ω"),
            ("额定端子纹波峰峰值", f"上限 {10*vset*req['ripple']['max_pct']:g} mVpp，"
                                    f"目标 {10*vset*req['ripple']['target_pct']:g} mVpp，短地线、计划 20 MHz 限带"),
            ("平均模型额定效率 / 最差工况", f"{100*nominal['eta_total']:.2f}% / {100*loss['envelope']['eta_min']:.2f}%"),
            ("容差谐波纹波估计", f"{loss['ripple']['vpp_harmonic_mv']:g} mVpp"),
            ("CV PI 初值", f"Kp={loop['CV'].get('Kp')}，Ki={loop['CV'].get('Ki')}"),
            ("CC PI 初值", f"Kp={loop['CC'].get('Kp')}，Ki={loop['CC'].get('Ki')}"),
        )),
        "", "### 电流精度预算", "",
        md_table(("已列项", "误差 mA", "器件和手册条件"), (
            (row["item"], f"{row['error_ma']:.2f}" if row["error_ma"] is not None else "缺证据",
             f"{row['source']} · {row['condition'] or '条件待查'}") for row in budget["terms"])),
        f"课程允许总误差 ±{budget['allowance_ma']:.2f} mA。"
        + (f"条件性小计 {budget['partial_ma']:.2f} mA，余量 {budget['remaining_ma']:.2f} mA。"
           if budget["partial_ma"] is not None else "小计待补证。"),
        "待补 " + "、".join(budget["pending"]) + "。",
        "", "### 模型检查和器件证据", "",
    ])
    for row in chk["checks"]:
        if row["id"] in {"SR54", "SR54-PTC", "SR70", "SR71"} and row["verdict"] != "N/A":
            lines.append(f"- {row['id']} `{row['verdict']}`。{row['detail']}")
    if sim:
        counts = Counter(c["verdict"] for c in sim["cases"])
        lines.append("- 开关模型设计工况 " + " · ".join(f"{k} {counts[k]}" for k in ("PASS", "WARN", "FAIL"))
                     + f"。另有 {len(sim.get('reference_cases', []))} 个对照工况。")
        lines.append("- 通过判据按工况分别取自 [sim.json](sim.json) 的 `checks` 字段。")
        evidence = [(c["name"], c.get("waveform"), c.get("metrics", {})) for c in sim["cases"]]
        for name, label in (("S-rated", "额定点"), ("S-ripple-corner", "容差纹波工况")):
            for case in sim["cases"]:
                if case["name"] == name and "ripple_vpp_v" in case["metrics"]:
                    lines.append(f"- {label}开关模型纹波 {1000*case['metrics']['ripple_vpp_v']:.2f} mVpp"
                                 f"（Vin={case['vin_v']:g} V，{case['variant']}）。")
        starts = [(n, path, m) for n, path, m in evidence if n == "S-start" and "il_peak_a" in m]
        if starts:
            _, path, metrics = max(starts, key=lambda item: item[2]["il_peak_a"])
            lines.append(f"- 满输入启动仿真的最大线性电感峰值 {metrics['il_peak_a']:.3f} A。"
                         f"超过 Isat 时应保留饱和风险。见 [{path}]({path})。")
    lines.extend(["", md_table(("用途", "器件资料", "数量", "封装", "立创编号"), parts),
                  "", "### 台架验收计划", "",
                  md_table(("项目", "测试条件", "验收判据"),
                           ((t["item"], t["condition"], t["criterion"]) for t in verification_plan(req, calc))),
                  "", "[规则记录](check.json) · [输入保护计划](input_protection_plan.json) · "
                  "[选型记录](selection.json) · [计算账本](ledger.md)"])
    return "\n".join(lines) + "\n"


def render_document(paths, db, source=None):
    report = "# A 模块系统设计与交接评审\n\n" + render_review(paths, db)
    if source is not None:
        report += "\n## 系统设计方案说明\n\n" + render(source, paths)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--db", type=Path, default=KNOWLEDGE_DIR / "parts_db.json")
    args = parser.parse_args()
    source = args.source or args.workdir / TEMPLATE
    output = args.workdir / REPORT
    try:
        if source.resolve() == output.resolve():
            raise ValueError("报告源和输出必须是不同文件")
        paths = workdir_paths(args.workdir)
        template = source.read_text(encoding="utf-8-sig") if args.source or source.is_file() else None
        rendered = render_document(paths, load_json(args.db), template)
        output.write_text(rendered, encoding="utf-8")
    except (OSError, ValueError) as err:
        parser.exit(1, f"报告渲染失败：{err}\n")
    print(f"{output.name} 已生成")


if __name__ == "__main__":
    main()
