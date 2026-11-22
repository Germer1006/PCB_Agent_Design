"""Record which implementation provides the input ramp and fault limiting."""

import argparse
import math
from pathlib import Path

from _common import load_json, save_json, workdir_paths


def positive(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def meaningful(value):
    return isinstance(value, str) and bool(value.strip())


def describe(choices):
    protection = choices["protection"]
    implementation = protection.get("input_protection", {})
    if not isinstance(implementation, dict):
        raise ValueError("protection.input_protection 必须为对象")
    strategy = implementation.get("strategy", "undecided")
    if strategy not in ("undecided", "board", "external"):
        raise ValueError("输入保护 strategy 必须为 undecided、board 或 external")
    ramp = protection.get("input_ramp")
    if ramp is not None and (not isinstance(ramp, dict) or
                             not all(positive(ramp.get(key)) for key in
                                     ("max_slew_v_per_ms", "source_peak_budget_a"))):
        raise ValueError("独立输入缓升的速率与源电流预算必须为正且有限")
    missing = []
    if strategy == "undecided":
        missing.append("明确由板载电路还是固定外部供电装置实现输入保护")
    for key, label in (
        ("implementation", "缓升和限流器件及其在输入路径中的位置"),
        ("fault_action", "输出短路时对 L/D 直通路径的限流或切断动作与恢复条件"),
        ("pwm_enable_condition", "输入就绪前的 PWM 禁止及允许启动条件"),
    ):
        if not meaningful(implementation.get(key)):
            missing.append(label)
    limits = implementation.get("current_limit_a")
    if not (isinstance(limits, dict) and positive(limits.get("min")) and positive(limits.get("max"))
            and limits["min"] <= limits["max"]):
        missing.append("含容差的输入限流下限及上限")
    if not ramp:
        missing.append("输入电容前端的最大缓升斜率及源电流峰值预算")
    evidence = implementation.get("evidence")
    if not (isinstance(evidence, list) and evidence and all(map(meaningful, evidence))):
        missing.append("具体器件的手册条件、供电设定或电路依据")
    if strategy == "board" and not positive(implementation.get("added_path_drop_max_v")):
        missing.append("新增板载串联保护在正常负载和适用温度下的最大压降")
    if strategy == "external" and not meaningful(implementation.get("connection_conditions")):
        missing.append("外部电源设置、是否允许已带电插接及限制源端释放能量的接法")
    return {
        "schema": "boost-input-protection-plan/1",
        "revision": choices.get("revision"),
        "strategy": strategy,
        "status": "implementation_pending" if missing else "documented_requires_verification",
        "implementation": implementation,
        "missing": missing,
        "model_input_ramp": ramp,
        "verification": "台架核验实际斜率、源/电感峰值、PWM 禁止、故障直通路径、能量和开关 SOA。",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workdir", type=Path)
    args = parser.parse_args()
    try:
        plan = describe(load_json(workdir_paths(args.workdir)["choices"]))
    except (OSError, ValueError) as error:
        parser.exit(2, f"输入保护方案读取失败：{error}\n")
    save_json(args.workdir / "input_protection_plan.json", plan)
    print(f"输入保护方案：{plan['status']}")
    for item in plan["missing"]:
        print(f"- 待落实：{item}")


if __name__ == "__main__":
    main()
