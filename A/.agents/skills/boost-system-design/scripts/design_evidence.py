"""Conditioned protection and sensing evidence for SDRC and the handoff report."""

from _common import param
from input_protection import describe, positive


def selected(selection, db, role):
    """A missing database entry is missing evidence during review."""
    category = selection["categories"].get(role, {})
    part = next((p for p in db["parts"] if p["id"] == category.get("selected")), None)
    return part, category.get("selected_qty") or 1


def bound(part, name, column):
    value = (part or {}).get("params", {}).get(name)
    return value.get(column) if isinstance(value, dict) else None


def is_ptc(part):
    return (part or {}).get("category") == "input_protection_ptc"


def current_error_budget(req, calc, selection, db):
    csa, _ = selected(selection, db, "csa")
    shunt, _ = selected(selection, db, "shunt")
    target_ma = 1000 * calc["inputs"]["iout_cc_a"]
    resistance = calc["sensing"]["iout"]["shunt_ohm"]
    allowance = target_ma * req["iout"]["steady_err_pct"] / 100
    items = []
    for label, part, field, scale in (
        ("电流放大器输入失调", csa, "vos_uv", 1e-3 / resistance),
        ("电流放大器增益误差", csa, "gain_error_pct", target_ma / 100),
        ("分流电阻初始容差", shunt, "tolerance_pct", target_ma / 100),
    ):
        value = bound(part, field, "max")
        record = (part or {}).get("params", {}).get(field)
        items.append({
            "item": label, "error_ma": None if value is None else value * scale,
            "source": ((part or {}).get("mpn") or "未选器件"),
            "condition": record.get("condition") if isinstance(record, dict) else None,
            "datasheet_url": (part or {}).get("datasheet_url"),
        })
    subtotal = (sum(item["error_ma"] for item in items)
                if all(item["error_ma"] is not None for item in items) else None)
    return {
        "setpoint_ma": target_ma, "allowance_ma": allowance, "terms": items,
        "partial_ma": subtotal, "remaining_ma": None if subtotal is None else allowance - subtotal,
        "pending": ["ADC 与基准误差", "温漂与共模变化", "校准残差与控制稳态偏差"],
        "status": "partial_requires_verification",
    }


def add_input_protection_checks(checks, choices, selection, db, losses):
    part, _ = selected(selection, db, "fuse")
    peak = max(case["iin_avg_A"] for case in losses["corner_cases"])
    fuse_title = "普通保险丝标称熔化 I²t 与线性浪涌模型对照"
    if is_ptc(part):
        checks.not_applicable("SR54", fuse_title, "F1 为 PTC，采用 SR54-PTC", ref="PROT")
        params = part["params"]
        cold_hold = param(part, "hold_current_a")
        trip = params.get("time_to_trip_s", {})
        ambient = losses["assumptions"]["ambient_c"]
        hold = part.get("temperature_rerating_reference", {}).get(str(float(ambient)))
        hold_text = (f"手册参考曲线 {ambient:g} °C 保持电流 {hold:g} A，差额 {hold-peak:+.3f} A。"
                     if hold is not None else f"缺 {ambient:g} °C 的对应保持电流证据。")
        cold_text = f"手册 {params['hold_current_a']['condition']} 保持值 {cold_hold:g} A。" if cold_hold is not None else ""
        trip_text = (f"动作时间上限 {trip['max']:g} s（{trip.get('condition', '手册条件')}）。"
                     if trip.get("max") is not None else "")
        checks.add("SR54-PTC", "PTC 热态保持、动作时间与故障热状态配合",
                   None, f"模型最大输入平均电流 {peak:.3f} A。{hold_text}{cold_text}{trip_text}"
                   "待按实际器件温度、源限流和故障电流验证误动作、动作时间和故障热耗。", ref="PROT")
    else:
        fi = param(part, "melting_i2t_a2s")
        energy = losses["inrush"]["i2t_a2s"]
        detail = (f"标称 {fi:g} A²s，线性模型 {energy:g} A²s。"
                  "重复浪涌寿命和故障配合仍需核对。"
                  if fi is not None else "缺少所选保险丝的标称熔化 I²t。")
        checks.add("SR54", fuse_title, None if fi is None else fi >= 10 * energy,
                   detail, warn_only=True, ref="PROT")
    plan = describe(choices)
    plan["modeled_max_input_average_a"] = peak
    window = plan["implementation"].get("current_limit_a")
    low = window.get("min") if isinstance(window, dict) else None
    if plan["missing"]:
        ok, detail = None, "待落实：" + "；".join(plan["missing"])
    elif positive(low) and low <= peak:
        ok, detail = False, f"限流下限 {low:g} A 未高于模型最大连续输入平均电流 {peak:.3f} A。"
    else:
        ok, detail = True, "已定义实施、限流窗口和故障动作。台架验证由 SR53 跟踪。"
    checks.add("SR89", "输入保护实现与 B 原理图接口已定义", ok, detail, ref="PROT")
    return plan
