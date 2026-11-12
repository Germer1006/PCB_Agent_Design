"""阶段 5a — 关键参数计算（与具体器件无关的初算）.

读取 requirements.json + design_choices.json，输出 calc.json：
工作点、电感/电容需求、器件应力（供选型脚本使用）、占空比限幅、PWM/控制时序、
采样网络、保护阈值（含 ADC 码值）、CC 可调范围、辅助电源与 MCU 资源预算。

用法: python calc_boost.py <workdir>
"""

import argparse
import math
from pathlib import Path

from _common import (CALC_CONSTANTS, E24, Steps, ceil_to, constant_input, load_json, pick_at_least,
                     pick_nearest, r3, save_json, source_input, step_input, workdir_paths)


def validate_inputs(req, ch):
    """Reject impossible or invalid inputs before arithmetic or part selection."""
    numbers = {"Vin": req["vin"]["nom_v"], "Vout": req["vout"]["set_v"],
               "Iout": req["iout"]["cc_set_a"], "Pout": req["pout"]["rated_w"],
               "fsw": ch["fsw_hz"], "eta_assumed": ch["power_stage"]["eta_assumed"],
               "eta_worst": ch["power_stage"]["eta_worst"],
               "inductor_ripple_ratio": ch["power_stage"]["inductor_ripple_ratio"]}
    for name, value in numbers.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a finite positive number")
    tol = req["vin"]["tol_pct"]
    if isinstance(tol, bool) or not isinstance(tol, (int, float)) or not math.isfinite(tol) or not 0 <= tol < 100:
        raise ValueError("Vin tolerance must be in [0, 100) percent")
    if ch.get("topology", "async_boost") != "async_boost":
        raise ValueError("Only async_boost is supported by this calculation tool")
    if max(numbers["eta_assumed"], numbers["eta_worst"]) > 1:
        raise ValueError("Efficiency must be <= 1")
    limits = req.get("fsw", {}).get("range_hz")
    if limits and not limits[0] <= ch["fsw_hz"] <= limits[1]:
        raise ValueError("fsw is outside the requirement range")


def calc(req, ch):
    try:
        validate_inputs(req, ch)
    except (KeyError, TypeError) as err:
        raise ValueError(f"Missing or malformed requirement/choice field: {err}") from err
    S = Steps()
    ps, ctl, sen, pro, aux = ch["power_stage"], ch["control"], ch["sensing"], ch["protection"], ch["aux"]

    def choice(path, name=None):
        return source_input("choice", ch, path, name)

    # ---------- 1. 需求基线 ----------
    pct = CALC_CONSTANTS["percent_divisor"]
    uh_per_h = CALC_CONSTANTS["uh_per_h"]
    vin_nom, tol_pct = req["vin"]["nom_v"], req["vin"]["tol_pct"]
    tol = tol_pct / pct
    vin = {"min": vin_nom * (1 - tol), "nom": vin_nom, "max": vin_nom * (1 + tol)}
    vo, io = req["vout"]["set_v"], req["iout"]["cc_set_a"]
    fsw = ch["fsw_hz"]
    eta, eta_w, vf = ps["eta_assumed"], ps["eta_worst"], ps["diode_vf_assumed_v"]

    vo_input = step_input("Vo", vo, "req:vout.set_v")
    io_input = step_input("Io", io, "req:iout.cc_set_a")
    fsw_input = choice("fsw_hz", "fsw")
    vf_input = choice("power_stage.diode_vf_assumed_v", "VF")
    percent_input = constant_input("percent_divisor")
    half_input = constant_input("half_cycle")
    eta_input = step_input("η", eta, "choice:power_stage.eta_assumed")
    vin_inputs = [step_input("Vnom", vin_nom, "req:vin.nom_v"),
                  step_input("tol_pct", tol_pct, "req:vin.tol_pct"),
                  step_input("百分比换算", pct, "const:percent_divisor")]
    po = S.add("P1", "额定输出功率", "Po = Vo·Io", f"{vo}×{io}", vo * io, "W",
               inputs=[vo_input, step_input("Io", io, "req:iout.cc_set_a")])
    rl = S.add("P2", "额定负载电阻（CV/CC 交界）", "R = Vo/Io", f"{vo}/{io}", vo / io, "Ω",
               inputs=[vo_input, io_input])
    S.add("P3", "输入电压下限", "Vin_min = Vnom·(1−tol_pct/100)", f"{vin_nom}×(1−{tol})",
          vin["min"], "V", f"min={vin['min']:.3f} V, max={vin['max']:.3f} V",
          inputs=vin_inputs)
    S.add("P3_MAX", "输入电压上限", "Vin_max = Vnom·(1+tol_pct/100)", f"{vin_nom}×(1+{tol})",
          vin["max"], "V", inputs=vin_inputs)
    if abs(po - req["pout"]["rated_w"]) / req["pout"]["rated_w"] > 0.05:
        raise ValueError("Pout rated conflicts with Vout*Iout")
    if vin["max"] >= vo:
        raise ValueError("Boost requires Vin_max < Vout")

    # ---------- 2. 工作点 ----------
    op = {}
    for k, v in vin.items():
        d_id = 1 - v / vo
        d_est = 1 - v * eta / vo
        iin = po / (eta * v)
        op[k] = {"vin_v": v, "d_ideal": d_id, "d_est": d_est, "iin_avg_a": iin}
    vin_sources = {"min": "step:P3", "nom": "req:vin.nom_v", "max": "step:P3_MAX"}
    vin_at = {k: step_input("Vin_" + k, v, vin_sources[k]) for k, v in vin.items()}
    suffixes = {"min": "", "nom": "_NOM", "max": "_MAX"}
    S.add("D1", "理想占空比 (Vin_min)", "D = 1 − Vin/Vo", f"1 − {vin['min']:.3f}/{vo}",
          op["min"]["d_ideal"], "", inputs=[vin_at["min"], vo_input])
    for k, o in op.items():
        suffix = "" if k == "min" else "_" + k.upper()
        vin_input = step_input("Vin", o["vin_v"], vin_sources[k])
        S.add("D2" + suffix, f"计及效率的占空比 (Vin_{k})", "D ≈ 1 − η·Vin/Vo",
              f"1 − {eta}×{o['vin_v']:.3f}/{vo}", o["d_est"], "",
              f"η={eta} 为初算假设，阶段 7 用实际器件复核",
              inputs=[eta_input, vin_input, vo_input])
        S.add("I1" + suffix, f"输入平均电流 (Vin_{k})", "Iin = Po/(η·Vin)",
              f"{po}/({eta}×{o['vin_v']:.3f})", o["iin_avg_a"], "A",
              "电感平均电流等于功率级输入平均电流",
              inputs=[step_input("Po", po, "step:P1"), eta_input, vin_input])

    # ---------- 3. 电感 ----------
    r = ps["inductor_ripple_ratio"]
    lreq = {k: o["vin_v"] * o["d_est"] / (fsw * r * o["iin_avg_a"]) for k, o in op.items()}
    worst_k = max(lreq, key=lreq.get)
    corner_inputs = []
    for k, o in op.items():
        suffix = "" if k == "min" else "_" + k.upper()
        corner_id = "L1_" + k.upper()
        value = S.add(
            corner_id, f"电感量需求 (Vin_{k})", "L = Vin·D/(fsw·r·Iin) × 10⁶",
            f"{o['vin_v']:.3f}×{o['d_est']:.3f}/({fsw:.0f}×{r}×{o['iin_avg_a']:.3f}) × 10⁶",
            lreq[k] * uh_per_h, "µH",
            inputs=[
                step_input("Vin", o["vin_v"], vin_sources[k]),
                step_input("D", o["d_est"], "step:D2" + suffix),
                step_input("fsw", fsw, "choice:fsw_hz"),
                step_input("r", r, "choice:power_stage.inductor_ripple_ratio"),
                step_input("Iin", o["iin_avg_a"], "step:I1" + suffix),
                step_input("µH/H", uh_per_h, "const:uh_per_h"),
            ],
        )
        corner_inputs.append(step_input(f"L_{k}", value, "step:" + corner_id))
    l_req = S.add("L1", "电感量需求（最坏角）", "L_req = max(L_min, L_nom, L_max)",
                  "max(" + ", ".join(f"{x['value']:.4g}" for x in corner_inputs) + ")",
                  lreq[worst_k] * uh_per_h, "µH", f"最坏角 Vin_{worst_k}", inputs=corner_inputs)
    rounding = ps.get("inductor_rounding_allowance", CALC_CONSTANTS["default_inductor_rounding"])
    available = [v for v in ps["inductor_std_values_uh"] if v >= l_req * (1 - rounding)]
    if not available:
        raise ValueError(f"No standard inductance satisfies L_req={l_req:.4g} uH; extend choices, then rerun S5")
    l_sel_uh = min(available)
    rounding_source = ("choice:power_stage.inductor_rounding_allowance"
                       if "inductor_rounding_allowance" in ps else "const:default_inductor_rounding")
    S.add("L2", "电感标称值（取标准值）", "取 ≥ (1−rounding_allowance)·L_req 的最近标准值", f"{l_req:.1f} µH → {l_sel_uh} µH", l_sel_uh, "µH",
          f"标准值向下取整允许 {rounding:.0%}；r 为初算目标，负容差下实际纹波比须另行核查",
          inputs=[step_input("L_req", l_req, "step:L1"),
                  step_input("rounding_allowance", rounding, rounding_source),
                  step_input("标准电感量", ps["inductor_std_values_uh"], "choice:power_stage.inductor_std_values_uh")])
    l_min = l_sel_uh * 1e-6 * (1 - ps["inductor_tolerance"])
    ripple_inputs, peak_inputs, rms_inputs, duty_inputs = {}, {}, {}, {}
    for k, o in op.items():
        dI = o["vin_v"] * o["d_est"] / (fsw * l_min)
        o["dIL_pp_a"] = dI
        o["il_pk_a"] = o["iin_avg_a"] + dI / 2
        o["il_valley_a"] = o["iin_avg_a"] - dI / 2
        o["il_rms_a"] = math.sqrt(o["iin_avg_a"] ** 2 + dI ** 2 / 12)
        o["ripple_ratio"] = dI / o["iin_avg_a"]
        suffix = suffixes[k]
        duty_inputs[k] = step_input(f"D_{k}", o["d_est"], "step:D2" + suffix)
        avg_input = step_input(f"Iin_{k}", o["iin_avg_a"], "step:I1" + suffix)
        S.add("L3" + suffix, f"电感纹波 (Vin_{k}, L 取 −容差)", "ΔIL = Vin·D/(fsw·L·(1−tol))",
              f"{o['vin_v']:.3f}×{o['d_est']:.3f}/({fsw:.0f}×{l_min*1e6:.1f}µ)", dI, "A",
              inputs=[vin_at[k], duty_inputs[k], fsw_input, step_input("L", l_sel_uh, "step:L2"),
                      choice("power_stage.inductor_tolerance"), constant_input("uh_per_h")])
        ripple_inputs[k] = step_input(f"ΔIL_{k}", dI, "step:L3" + suffix)
        peak_inputs[k] = [avg_input, ripple_inputs[k]]
        S.add("IL_RMS_" + k.upper(), f"电感 RMS 电流 (Vin_{k})", "IL,rms = √(Iin² + ΔIL²/12)",
              f"√({o['iin_avg_a']:.4g}² + {dI:.4g}²/12)", o["il_rms_a"], "A",
              inputs=[*peak_inputs[k], constant_input("triangle_rms_divisor")])
        rms_inputs[k] = step_input(f"IL,rms_{k}", o["il_rms_a"], "step:IL_RMS_" + k.upper())
    ipk = max(o["il_pk_a"] for o in op.values())
    ilrms = max(o["il_rms_a"] for o in op.values())
    S.add("L4", "电感峰值电流（最坏）", "max(Iin + ΔIL/2)", "见各角", ipk, "A",
          inputs=[*(item for corner in peak_inputs.values() for item in corner), half_input])
    S.add("L5", "电感 RMS 电流（最坏）", "max(IL,rms)", "见各角", ilrms, "A", inputs=rms_inputs.values())
    ipk_input = step_input("IL,pk", ipk, "step:L4")
    isat_req = S.add("L6", "电感饱和电流需求", "Isat ≥ k·IL,pk", f"{ps['isat_margin']}×{ipk:.3f}",
                     ps["isat_margin"] * ipk, "A", inputs=[choice("power_stage.isat_margin"), ipk_input])
    irms_req = S.add("L7", "电感温升电流需求", "Irms ≥ k·IL,rms", f"{ps['irms_margin']}×{ilrms:.3f}",
                     ps["irms_margin"] * ilrms, "A",
                     inputs=[choice("power_stage.irms_margin"), step_input("IL,rms", ilrms, "step:L5")])
    d_nom = op["nom"]["d_est"]
    io_crit = op["nom"]["dIL_pp_a"] / 2 * (1 - d_nom)
    S.add("L8", "CCM/DCM 临界输出电流 (Vin_nom)", "Io,crit = ΔIL/2·(1−D)",
          f"{op['nom']['dIL_pp_a']:.3f}/2×(1−{d_nom:.3f})", io_crit, "A",
          f"初算负载 < {io_crit*1000:.0f} mA（R > {vo/io_crit:.0f} Ω）可能进入 DCM，CCM模型不适用，须另行验证",
          inputs=[ripple_inputs["nom"], half_input, duty_inputs["nom"]])

    # ---------- 4. 保护阈值（先算，供应力使用） ----------
    v_ovp_sw = vo * (1 + pro["ovp_sw_pct"] / 100)
    v_ovp_hw = vo * (1 + pro["ovp_hw_pct"] / 100)
    i_ocp_sw = io * (1 + pro["ocp_sw_pct"] / 100)
    i_ocp_hw = pro["ocp_hw_a"]
    S.add("PR2", "硬件过压阈值", "Vovp,hw = Vo·(1+y%)", f"{vo}×(1+{pro['ovp_hw_pct']}%)", v_ovp_hw, "V",
          inputs=[vo_input, choice("protection.ovp_hw_pct"), percent_input])
    hw_ovp_step = S.items[-1]
    S.add("PR4", "硬件过流阈值", "设定值", f"{i_ocp_hw}", i_ocp_hw, "A",
          f"仅按稳态换算的电感平均电流约 {i_ocp_hw/(1-op['min']['d_est']):.2f} A，不是故障瞬态上界；须复核饱和与保护协调",
          inputs=[choice("protection.ocp_hw_a")])
    ovp_input = step_input("Vovp,hw", v_ovp_hw, "step:PR2")

    # ---------- 5. 开关管 / 二极管应力 ----------
    k_v = ps["voltage_derating"]
    stress_inputs = [choice("power_stage.voltage_derating"), ovp_input]
    v_sw = v_ovp_hw + vf
    vds_req = S.add("Q1", "MOSFET 耐压需求", "Vds ≥ k·(Vovp,hw + VF)", f"{k_v}×({v_ovp_hw:.2f}+{vf})", k_v * v_sw, "V",
                    "以硬件过压点而非额定 12 V 作为应力基准", inputs=[*stress_inputs, vf_input])
    iq_rms = max(math.sqrt(o["d_est"]) * o["il_rms_a"] for o in op.values())
    S.add("Q2", "MOSFET RMS 电流", "IQ,rms ≈ max(√D·IL,rms)", "最坏角", iq_rms, "A",
          inputs=[*duty_inputs.values(), *rms_inputs.values()])
    vr_req = S.add("D3", "二极管反向耐压需求", "VR ≥ k·Vovp,hw", f"{k_v}×{v_ovp_hw:.2f}", k_v * v_ovp_hw, "V",
                   inputs=stress_inputs)
    S.add("D4", "二极管平均电流", "ID,avg = Io", f"{io}", io, "A", f"峰值 = IL,pk = {ipk:.2f} A", inputs=[io_input])
    S.add("D5", "二极管导通损耗初估", "PD ≈ VF·Io", f"{vf}×{io}", vf * io, "W", inputs=[vf_input, io_input])

    # ---------- 6. 输出/输入电容 ----------
    dvo = vo * ps["ripple_design_pct"] / 100
    d_max_op = max(o["d_est"] for o in op.values())
    ripple_budget = [vo_input, choice("power_stage.ripple_design_pct"), percent_input, half_input]
    milli_input = constant_input("milli_per_unit")
    micro_input = constant_input("uh_per_h")
    c_req = S.add("C1", "输出电容有效容量需求", "C ≥ Io·D/(fsw·ΔVc)，ΔVc = ΔVo/2",
                  f"{io}×{d_max_op:.3f}/({fsw:.0f}×{dvo/2:.3f})", io * d_max_op / (fsw * dvo / 2) * 1e6, "µF",
                  f"设计纹波 {ps['ripple_design_pct']}%（{dvo*1000:.0f} mV）留余量于课程 ≤3% 建议值；电容放电与 ESR 各占一半",
                  inputs=[io_input, *duty_inputs.values(), fsw_input, *ripple_budget, micro_input])
    esr_max = S.add("C2", "输出电容 ESR 上限", "ESR ≤ (ΔVo/2)/IL,pk", f"{dvo/2:.3f}/{ipk:.3f}", dvo / 2 / ipk * 1e3, "mΩ",
                    "二极管电流在 0 与 IL,pk 间跳变，ESR 纹波 = IL,pk·ESR",
                    inputs=[*ripple_budget, ipk_input, milli_input])
    ic_rms = S.add("C3", "输出电容纹波电流", "IC,rms ≈ Io·√(D/(1−D))", f"{io}×√({d_max_op:.3f}/{1-d_max_op:.3f})",
                   io * math.sqrt(d_max_op / (1 - d_max_op)), "A", inputs=[io_input, *duty_inputs.values()])
    vc_req = S.add("C4", "输出电容耐压需求", "VC ≥ k·Vovp,hw", f"{k_v}×{v_ovp_hw:.2f}", k_v * v_ovp_hw, "V",
                   inputs=stress_inputs)
    dvin = vin_nom * ps["vin_ripple_design_pct"] / 100
    dIL_max = max(o["dIL_pp_a"] for o in op.values())
    cin_req = S.add("C5", "输入电容需求", "Cin ≥ ΔIL/(8·fsw·ΔVin)", f"{dIL_max:.3f}/(8×{fsw:.0f}×{dvin:.3f})",
                    dIL_max / (8 * fsw * dvin) * 1e6, "µF", "Boost 输入电流连续，仅需滤除三角纹波",
                    inputs=[*ripple_inputs.values(), fsw_input, vin_at["nom"], choice("power_stage.vin_ripple_design_pct"),
                            percent_input, micro_input, constant_input("input_ripple_divisor")])

    # ---------- 7. 占空比限幅 ----------
    d_worst = 1 - vin["min"] * eta_w / vo
    S.add("D6", "最坏占空比需求", "D = 1 − ηworst·Vin,min/Vo", f"1 − {eta_w}×{vin['min']:.3f}/{vo}", d_worst, "",
          inputs=[choice("power_stage.eta_worst"), vin_at["min"], vo_input])
    dmax = S.add("D7", "占空比上限 Dmax", "Dmax = ceil(Dworst + margin, step)",
                 f"ceil({d_worst:.3f}+{ch['duty']['dmax_margin']}, {ch['duty']['dmax_step']})",
                 ceil_to(d_worst + ch["duty"]["dmax_margin"], ch["duty"]["dmax_step"]), "",
                 "阶段 7 用实际寄生参数核对 Dmax 低于升压比峰值点",
                 inputs=[step_input("Dworst", d_worst, "step:D6"), choice("duty.dmax_margin"), choice("duty.dmax_step")])
    dmin = ch["duty"]["dmin"]

    # ---------- 8. PWM 与控制时序 ----------
    ftim = ctl["timer_clk_hz"]
    arr = ftim / fsw / (2 if ctl["pwm_alignment"] == "center" else 1)
    S.add("T1", "PWM 周期计数值", "N = ftim/fsw" + ("/2" if ctl["pwm_alignment"] == "center" else ""),
          f"{ftim:.0f}/{fsw:.0f}", arr, "count", f"ARR = {int(round(arr))-1}；分辨率 {math.log2(arr):.1f} bit",
          inputs=[choice("control.timer_clk_hz"), fsw_input, choice("control.pwm_alignment"), half_input])
    gvd_nom = vo / (1 - d_nom)
    S.add("T2", "1 LSB 占空比对应输出电压步长", "ΔVo = (Vo/(1−D))/N", f"({vo}/(1−{d_nom:.3f}))/{arr:.0f}",
          gvd_nom / arr * 1e3, "mV", "须远小于 ±5% 精度要求",
          inputs=[vo_input, duty_inputs["nom"], step_input("N", arr, "step:T1"), milli_input])
    ccr_max = math.floor(dmax * arr)
    rcr = fsw / ctl["f_ctrl_hz"] - 1
    S.add("T3", "控制周期", "Tc = 1/fctrl", f"1/{ctl['f_ctrl_hz']}", 1e6 / ctl["f_ctrl_hz"], "µs",
          f"TIM1 重复计数 RCR = {rcr:.0f}，每 {rcr+1:.0f} 个 PWM 周期更新一次",
          inputs=[choice("control.f_ctrl_hz"), micro_input])
    cpu_load = S.add("T4", "控制中断 CPU 占用", "load = cycles·fctrl/fcpu",
                     f"{ctl['isr_cycles_est']}×{ctl['f_ctrl_hz']}/{ctl['cpu_clk_hz']:.0f}",
                     ctl["isr_cycles_est"] * ctl["f_ctrl_hz"] / ctl["cpu_clk_hz"] * 100, "%",
                     inputs=[choice("control." + k) for k in ("isr_cycles_est", "f_ctrl_hz", "cpu_clk_hz")]
                     + [percent_input])
    cm = ch["comms"]
    uart_load = S.add("T5", "调试串口带宽占用", "load = bytes·10·rate/baud",
                      f"{cm['log_frame_bytes']}×10×{cm['log_rate_hz']}/{cm['uart_baud']}",
                      cm["log_frame_bytes"] * 10 * cm["log_rate_hz"] / cm["uart_baud"] * 100, "%",
                      inputs=[choice("comms." + k) for k in ("log_frame_bytes", "log_rate_hz", "uart_baud")]
                      + [constant_input("uart_bits_per_byte"), percent_input])

    # ---------- 9. 采样网络 ----------
    nbits, vref = sen["adc_bits"], sen["adc_vref_v"]
    full = 2 ** nbits - 1
    v_use = vref * sen["adc_usable_frac"]

    adc_reference = choice("sensing.adc_vref_v")
    adc_bits = choice("sensing.adc_bits")

    def divider(v_fs_target, sid, label, inputs):
        rb = sen["divider_rbot_ohm"]
        rt = pick_at_least(rb * (v_fs_target / v_use - 1), E24)
        k = rb / (rt + rb)
        fs_v = vref / k
        rth = rt * rb / (rt + rb)
        cf = pick_nearest(1 / (2 * math.pi * rth * sen["divider_filter_fc_hz"]))
        S.add(sid, f"{label} 分压比", "k = Rb/(Rt+Rb)，Rt 取 E24", f"Rb={rb:.0f}, Rt={rt:.0f}", k, "",
              f"ADC 满量程对应 {fs_v:.2f} V；滤波电容 {cf*1e9:.3g} nF（fc≈{1/(2*math.pi*rth*cf):.0f} Hz）",
              inputs=[*inputs, adc_reference, choice("sensing.adc_usable_frac"),
                      choice("sensing.divider_rbot_ohm"), constant_input("e24")])
        if sid == "S1":
            S.add("S1_FILT", "输出电压采样滤波极点", "fc = 1/(2π·Rth·Cf)，Cf 取 E6",
                  f"Rth={rth:.4g} Ω, Cf={cf:.4g} F", 1 / (2 * math.pi * rth * cf), "Hz",
                  inputs=[step_input("k", k, "step:S1"), choice("sensing.divider_rbot_ohm"),
                          choice("sensing.divider_filter_fc_hz"), constant_input("e6"),
                          constant_input("pi"), half_input])
        return {"r_top_ohm": rt, "r_bot_ohm": rb, "ratio": k, "fullscale_v": fs_v,
                "filter_c_f": cf, "filter_fc_hz": 1 / (2 * math.pi * rth * cf), "r_thevenin_ohm": rth}

    vdiv = divider(v_ovp_hw * sen["vout_fs_over_ovp_hw"], "S1", "输出电压",
                   [ovp_input, choice("sensing.vout_fs_over_ovp_hw")])
    vdiv["lsb_mv"] = vdiv["fullscale_v"] / full * 1e3
    vdiv["code_at_vset"] = round(vo * vdiv["ratio"] / vref * full)
    vdiv["divider_loss_w"] = vo ** 2 / (vdiv["r_top_ohm"] + vdiv["r_bot_ohm"])
    S.add("S2", "输出电压分辨率", "LSB = VFS/(2^N−1)", f"{vdiv['fullscale_v']:.2f}/{full}", vdiv["lsb_mv"], "mV",
          f"12 V 对应码值 {vdiv['code_at_vset']}",
          inputs=[step_input("k", vdiv["ratio"], "step:S1"), adc_reference, adc_bits, milli_input])
    idiv = divider(vin["max"] * sen["vin_fs_over_vin_max"], "S3", "输入电压",
                   [vin_at["max"], choice("sensing.vin_fs_over_vin_max")])

    v_amp_max = min(vref, sen["csa_supply_v"] - sen["csa_swing_to_rail_v"])
    best = None
    for g in sen["csa_gain_options"]:
        for rs in sen["shunt_options_ohm"]:
            i_fs = v_amp_max / (rs * g)
            loss = rs * i_ocp_hw ** 2
            if i_fs < sen["iout_fs_min_a"] or loss > sen["shunt_max_loss_w"]:
                continue
            key = (rs * g, rs)
            if best is None or key > best[0]:
                best = (key, g, rs, i_fs)
    if best is None:
        raise ValueError("No shunt/gain combination meets current-sense full-scale and loss constraints")
    _, g, rs, i_fs = best
    S.add("S4", "电流采样电阻 × 放大倍数", "选 max(Rs·G) 使 IFS ≥ IFS,min 且 Rs·Iocp² ≤ Ploss,max",
          f"Rs={rs*1e3:.0f} mΩ, G={g}", rs * g, "V/A",
          f"满量程 {i_fs:.2f} A；0.5 A 时采样电压 {rs*io*1e3:.0f} mV，ADC 输入 {rs*g*io:.2f} V",
          inputs=[adc_reference, step_input("Iocp", i_ocp_hw, "step:PR4"),
                  *(choice("sensing." + k) for k in ("csa_supply_v", "csa_swing_to_rail_v",
                    "csa_gain_options", "shunt_options_ohm", "iout_fs_min_a", "shunt_max_loss_w"))])
    isen = {"location": sen["current_sense_location"], "shunt_ohm": rs, "gain": g, "transimpedance_v_per_a": rs * g,
            "fullscale_a": i_fs, "adc_fullscale_a": vref / (rs * g), "lsb_ma": vref / (rs * g) / full * 1e3, "code_at_icc": round(io * rs * g / vref * full),
            "shunt_loss_rated_w": rs * io ** 2, "v_sense_at_icc_mv": rs * io * 1e3}
    S.add("S5", "输出电流分辨率", "LSB = Vref/(Rs·G)/(2^N−1)", f"{vref}/({rs}×{g})/{full}", isen["lsb_ma"], "mA",
          f"0.5 A 对应码值 {isen['code_at_icc']}",
          inputs=[adc_reference, adc_bits, step_input("Rs·G", rs * g, "step:S4"), milli_input])

    # ---------- 10. 保护阈值与码值 ----------
    def vcode(v):
        return round(v * vdiv["ratio"] / vref * full)

    def icode(i):
        return round(i * rs * g / vref * full)

    def vincode(v):
        return round(v * idiv["ratio"] / vref * full)

    prot = {
        "ovp_sw": {"value": v_ovp_sw, "unit": "V", "adc_code": vcode(v_ovp_sw), "action": "软件关断 PWM 并锁存故障"},
        "ovp_hw": {"value": v_ovp_hw, "unit": "V", "adc_code": vcode(v_ovp_hw),
                   "action": "片内比较器 + 独立阈值源 → TIM1 刹车并锁存；关闭自动重使能，显式复位后才允许启动"},
        "ocp_sw": {"value": i_ocp_sw, "unit": "A", "adc_code": icode(i_ocp_sw), "delay_ms": pro["ocp_sw_delay_ms"],
                   "action": "持续超限后关断 PWM 并锁存（CC 环正常时不会触发）"},
        "ocp_hw": {"value": i_ocp_hw, "unit": "A", "adc_code": icode(i_ocp_hw),
                   "action": "输出电流比较器 → TIM1 刹车并锁存；不是电感逐周期峰值限流，不切断输入直通路径"},
        "uvlo": {"off_v": pro["uvlo_off_v"], "on_v": pro["uvlo_on_v"],
                 "adc_code_off": vincode(pro["uvlo_off_v"]), "adc_code_on": vincode(pro["uvlo_on_v"]),
                 "action": "输入欠压停机并锁存；回差恢复仅允许显式复位、自检、软启动，不自动重启"},
        "duty_limit": {"dmin": dmin, "dmax": dmax, "ccr_max": ccr_max, "action": "PI 输出限幅 + 定时器比较值钳位"},
        "soft_start": {"ramp_ms": ctl["soft_start_ms"],
                       "action": "参考电压从当前 Vout 斜坡升至设定值，控制开关加能；不限制插电时经 L/D 的直通浪涌"},
    }
    if pro.get("input_ramp"):
        prot["input_ramp"] = dict(pro["input_ramp"])
    S.add("PR1", "软件过压阈值", "Vovp,sw = Vo·(1+x%)", f"{vo}×(1+{pro['ovp_sw_pct']}%)", v_ovp_sw, "V",
          f"ADC 码值 {prot['ovp_sw']['adc_code']}", inputs=[vo_input, choice("protection.ovp_sw_pct"), percent_input])
    hw_ovp_step["note"] = f"同一电压在 ADC 通道的等效码值 {prot['ovp_hw']['adc_code']}；不是已验证 DAC 阈值码"
    S.add("PR3", "软件过流阈值", "Iocp,sw = Icc·(1+z%)", f"{io}×(1+{pro['ocp_sw_pct']}%)", i_ocp_sw, "A",
          inputs=[io_input, choice("protection.ocp_sw_pct"), percent_input])

    # ---------- 11. CC 可调范围 / 短路边界 ----------
    vo_cc_min = vin["max"] + vf + pro["cc_vout_min_margin_v"]
    S.add("CC1", "CC 模式最低可调输出电压", "Vo,cc,min = Vin,max + VF + margin",
          f"{vin['max']:.2f}+{vf}+{pro['cc_vout_min_margin_v']}", vo_cc_min, "V",
          "Boost 关断时仍有输入经 L/D 的直通路径，电压随器件压降及负载变化；本规划式不是物理下限，也不保证短路恒流",
          inputs=[vin_at["max"], vf_input, choice("protection.cc_vout_min_margin_v")])
    r_cc_min = S.add("CC2", "CC 模式最小负载电阻", "Rmin = Vo,cc,min/Icc", f"{vo_cc_min:.2f}/{io}", vo_cc_min / io, "Ω",
                     f"CR 测试 CC 段负载范围 {vo_cc_min/io:.1f}–{rl:.0f} Ω；更低阻值或短路依靠输入保险丝/电源限流",
                     inputs=[step_input("Vo,cc,min", vo_cc_min, "step:CC1"), io_input])

    # ---------- 12. 辅助电源 ----------
    i3v3 = aux["mcu_run_ma"] + aux["csa_ma"] + aux["led_count"] * aux["led_ma"] + aux["misc_ma"]
    S.add("A1", "3.3 V 负载电流", "I = IMCU + ICSA + n·ILED + Imisc",
          f"{aux['mcu_run_ma']}+{aux['csa_ma']}+{aux['led_count']}×{aux['led_ma']}+{aux['misc_ma']}", i3v3, "mA",
          inputs=[choice("aux." + k) for k in ("mcu_run_ma", "csa_ma", "led_count", "led_ma", "misc_ma")])
    rail_input = constant_input("aux_rail_v")
    p_ldo = S.add("A2", "LDO 功耗", "P = (Vin,max − 3.3)·I", f"({vin['max']:.2f}−3.3)×{i3v3:.1f}m",
                  (vin["max"] - 3.3) * i3v3, "mW",
                  inputs=[vin_at["max"], rail_input, step_input("I3V3", i3v3, "step:A1")])
    S.add("A3", "LDO 允许压差", "Vdo ≤ Vin,min − 3.3", f"{vin['min']:.2f}−3.3", vin["min"] - 3.3, "V",
          "选 LDO 时其满载压差须小于该值并留裕量", inputs=[vin_at["min"], rail_input])
    p_aux = (vin["nom"] * i3v3 + vin["nom"] * aux["gate_driver_iq_ma"]) / 1000

    return {
        "schema": "boost-calc/1",
        "revision": ch.get("revision"),
        "inputs": {"vin": vin, "vout_v": vo, "iout_cc_a": io, "pout_w": po, "fsw_hz": fsw,
                   "eta_assumed": eta, "eta_worst": eta_w, "diode_vf_assumed_v": vf},
        "operating_points": {k: {kk: r3(vv) for kk, vv in o.items()} for k, o in op.items()},
        "inductor": {"rounding_allowance": rounding, "ripple_ratio_target": r,
                     "ripple_ratio_worst": r3(max(o["ripple_ratio"] for o in op.values())),
                     "ripple_target_met_at_min_l": max(o["ripple_ratio"] for o in op.values()) <= r,
                     "l_required_uh": r3(l_req), "l_selected_uh": l_sel_uh, "l_min_effective_uh": r3(l_min * 1e6),
                     "ipk_a": r3(ipk), "irms_a": r3(ilrms), "isat_required_a": r3(isat_req),
                     "irms_required_a": r3(irms_req), "io_crit_dcm_a": r3(io_crit)},
        "output_cap": {"c_eff_required_uf": r3(c_req), "esr_max_mohm": r3(esr_max), "ripple_current_a": r3(ic_rms),
                       "v_rating_required_v": r3(vc_req), "ripple_design_v": r3(dvo)},
        "input_cap": {"c_required_uf": r3(cin_req)},
        "stress": {"mosfet_vds_required_v": r3(vds_req), "mosfet_irms_a": r3(iq_rms), "mosfet_ipk_a": r3(ipk),
                   "diode_vr_required_v": r3(vr_req), "diode_iavg_a": io, "diode_ipk_a": r3(ipk)},
        "duty": {"d_worst": r3(d_worst), "dmax": r3(dmax), "dmin": dmin},
        "pwm": {"timer_clk_hz": ftim, "counts_per_period": r3(arr), "arr": int(round(arr)) - 1,
                "resolution_bits": r3(math.log2(arr), 3), "ccr_max": ccr_max,
                "vout_step_per_lsb_mv": r3(gvd_nom / arr * 1e3), "alignment": ctl["pwm_alignment"]},
        "timing": {"f_ctrl_hz": ctl["f_ctrl_hz"], "tim1_rcr": int(rcr), "cpu_load_pct": r3(cpu_load),
                   "uart_load_pct": r3(uart_load)},
        "sensing": {"adc_bits": nbits, "adc_vref_v": vref,
                    "vout": {k: r3(v) for k, v in vdiv.items()}, "vin": {k: r3(v) for k, v in idiv.items()},
                    "iout": {k: (r3(v) if isinstance(v, float) else v) for k, v in isen.items()}},
        "protection": prot,
        "cc_range": {"vo_min_v": r3(vo_cc_min), "r_min_ohm": r3(r_cc_min), "r_max_ohm": r3(rl),
                     "short_circuit": "非同步 Boost 无法限制短路电流，需输入保险丝/实验电源限流"},
        "aux": {"i_3v3_ma": r3(i3v3), "ldo_loss_mw": r3(p_ldo), "ldo_dropout_allow_v": r3(vin["min"] - 3.3),
                "p_aux_w": r3(p_aux)},
        "steps": S.items,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir")
    a = ap.parse_args()
    p = workdir_paths(a.workdir)
    try:
        out = calc(load_json(p["requirements"]), load_json(p["choices"]))
    except (ValueError, KeyError, TypeError) as err:
        save_json(Path(a.workdir) / "calc_error.json",
                  {"schema": "boost-stage-error/1", "stage": "S5", "reason": str(err)})
        ap.exit(2, f"S5 拒绝计算：{err}\n")
    (Path(a.workdir) / "calc_error.json").unlink(missing_ok=True)
    save_json(p["calc"], out)
    print(f"calc.json written: L={out['inductor']['l_selected_uh']} µH, Isat≥{out['inductor']['isat_required_a']} A, "
          f"Cout,eff≥{out['output_cap']['c_eff_required_uf']} µF, Dmax={out['duty']['dmax']}, "
          f"Rs={out['sensing']['iout']['shunt_ohm']} Ω × G{out['sensing']['iout']['gain']}")


if __name__ == "__main__":
    main()
