"""阶段 5a — 关键参数计算（与具体器件无关的初算）.

读取 requirements.json + design_choices.json，输出 calc.json：
工作点、电感/电容需求、器件应力（供选型脚本使用）、占空比限幅、PWM/控制时序、
采样网络、保护阈值（含 ADC 码值）、CC 可调范围、辅助电源与 MCU 资源预算。

用法: python calc_boost.py <workdir>
"""

import argparse
import math

from _common import E24, Steps, ceil_to, load_json, pick_at_least, pick_nearest, r3, save_json, workdir_paths


def calc(req, ch):
    S = Steps()
    ps, ctl, sen, pro, aux = ch["power_stage"], ch["control"], ch["sensing"], ch["protection"], ch["aux"]

    # ---------- 1. 需求基线 ----------
    vin_nom, tol = req["vin"]["nom_v"], req["vin"]["tol_pct"] / 100
    vin = {"min": vin_nom * (1 - tol), "nom": vin_nom, "max": vin_nom * (1 + tol)}
    vo, io = req["vout"]["set_v"], req["iout"]["cc_set_a"]
    fsw = ch["fsw_hz"]
    eta, eta_w, vf = ps["eta_assumed"], ps["eta_worst"], ps["diode_vf_assumed_v"]

    po = S.add("P1", "额定输出功率", "Po = Vo·Io", f"{vo}×{io}", vo * io, "W")
    rl = S.add("P2", "额定负载电阻（CV/CC 交界）", "R = Vo/Io", f"{vo}/{io}", vo / io, "Ω")
    S.add("P3", "输入电压范围", "Vin = Vnom·(1±tol)", f"{vin_nom}×(1±{tol})", vin["min"], "V",
          f"min={vin['min']:.3f} V, max={vin['max']:.3f} V")
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
    S.add("D1", "理想占空比 (Vin_min)", "D = 1 − Vin/Vo", f"1 − {vin['min']:.3f}/{vo}", op["min"]["d_ideal"], "")
    S.add("D2", "计及效率的占空比 (Vin_min)", "D ≈ 1 − η·Vin/Vo", f"1 − {eta}×{vin['min']:.3f}/{vo}",
          op["min"]["d_est"], "", f"η={eta} 为初算假设，阶段 7 用实际器件复核")
    S.add("I1", "输入平均电流 (Vin_min, 最大)", "Iin = Po/(η·Vin)", f"{po}/({eta}×{vin['min']:.3f})",
          op["min"]["iin_avg_a"], "A", "电感平均电流 = 输入电流，远大于输出 0.5 A")

    # ---------- 3. 电感 ----------
    r = ps["inductor_ripple_ratio"]
    lreq = {k: o["vin_v"] * o["d_est"] / (fsw * r * o["iin_avg_a"]) for k, o in op.items()}
    worst_k = max(lreq, key=lreq.get)
    l_req = S.add("L1", "电感量需求（最坏角）", "L ≥ Vin·D/(fsw·r·Iin)",
                  f"{vin[worst_k]:.3f}×{op[worst_k]['d_est']:.3f}/({fsw:.0f}×{r}×{op[worst_k]['iin_avg_a']:.3f})",
                  lreq[worst_k] * 1e6, "µH", f"r={r} 为纹波比；最坏角 Vin_{worst_k}")
    l_sel_uh = min(v for v in ps["inductor_std_values_uh"] if v >= l_req * 0.97)
    S.add("L2", "电感标称值（取标准值）", "取 ≥ L_req 的最近标准值", f"{l_req:.1f} µH → {l_sel_uh} µH", l_sel_uh, "µH")
    l_min = l_sel_uh * 1e-6 * (1 - ps["inductor_tolerance"])
    for k, o in op.items():
        dI = o["vin_v"] * o["d_est"] / (fsw * l_min)
        o["dIL_pp_a"] = dI
        o["il_pk_a"] = o["iin_avg_a"] + dI / 2
        o["il_valley_a"] = o["iin_avg_a"] - dI / 2
        o["il_rms_a"] = math.sqrt(o["iin_avg_a"] ** 2 + dI ** 2 / 12)
        o["ripple_ratio"] = dI / o["iin_avg_a"]
    ipk = max(o["il_pk_a"] for o in op.values())
    ilrms = max(o["il_rms_a"] for o in op.values())
    S.add("L3", "电感纹波 (Vin_min, L 取 −容差)", "ΔIL = Vin·D/(fsw·L·(1−tol))",
          f"{vin['min']:.3f}×{op['min']['d_est']:.3f}/({fsw:.0f}×{l_min*1e6:.1f}µ)", op["min"]["dIL_pp_a"], "A")
    S.add("L4", "电感峰值电流（最坏）", "IL,pk = Iin + ΔIL/2", "见各角", ipk, "A")
    S.add("L5", "电感 RMS 电流（最坏）", "IL,rms = √(Iin² + ΔIL²/12)", "见各角", ilrms, "A")
    isat_req = S.add("L6", "电感饱和电流需求", "Isat ≥ k·IL,pk", f"{ps['isat_margin']}×{ipk:.3f}",
                     ps["isat_margin"] * ipk, "A")
    irms_req = S.add("L7", "电感温升电流需求", "Irms ≥ k·IL,rms", f"{ps['irms_margin']}×{ilrms:.3f}",
                     ps["irms_margin"] * ilrms, "A")
    d_nom = op["nom"]["d_est"]
    io_crit = op["nom"]["dIL_pp_a"] / 2 * (1 - d_nom)
    S.add("L8", "CCM/DCM 临界输出电流 (Vin_nom)", "Io,crit = ΔIL/2·(1−D)",
          f"{op['nom']['dIL_pp_a']:.3f}/2×(1−{d_nom:.3f})", io_crit, "A",
          f"负载 < {io_crit*1000:.0f} mA（R > {vo/io_crit:.0f} Ω）进入 DCM，环路增益下降，需在环路设计中考虑")

    # ---------- 4. 保护阈值（先算，供应力使用） ----------
    v_ovp_sw = vo * (1 + pro["ovp_sw_pct"] / 100)
    v_ovp_hw = vo * (1 + pro["ovp_hw_pct"] / 100)
    i_ocp_sw = io * (1 + pro["ocp_sw_pct"] / 100)
    i_ocp_hw = pro["ocp_hw_a"]

    # ---------- 5. 开关管 / 二极管应力 ----------
    k_v = ps["voltage_derating"]
    v_sw = v_ovp_hw + vf
    vds_req = S.add("Q1", "MOSFET 耐压需求", "Vds ≥ k·(Vovp,hw + VF)", f"{k_v}×({v_ovp_hw:.2f}+{vf})", k_v * v_sw, "V",
                    "以硬件过压点而非额定 12 V 作为应力基准")
    iq_rms = max(math.sqrt(o["d_est"]) * o["il_rms_a"] for o in op.values())
    S.add("Q2", "MOSFET RMS 电流", "IQ,rms ≈ √D·IL,rms", "最坏角", iq_rms, "A")
    vr_req = S.add("D3", "二极管反向耐压需求", "VR ≥ k·Vovp,hw", f"{k_v}×{v_ovp_hw:.2f}", k_v * v_ovp_hw, "V")
    S.add("D4", "二极管平均电流", "ID,avg = Io", f"{io}", io, "A", f"峰值 = IL,pk = {ipk:.2f} A")
    S.add("D5", "二极管导通损耗初估", "PD ≈ VF·Io", f"{vf}×{io}", vf * io, "W")

    # ---------- 6. 输出/输入电容 ----------
    dvo = vo * ps["ripple_design_pct"] / 100
    d_max_op = max(o["d_est"] for o in op.values())
    c_req = S.add("C1", "输出电容有效容量需求", "C ≥ Io·D/(fsw·ΔVc)，ΔVc = ΔVo/2",
                  f"{io}×{d_max_op:.3f}/({fsw:.0f}×{dvo/2:.3f})", io * d_max_op / (fsw * dvo / 2) * 1e6, "µF",
                  f"设计纹波 {ps['ripple_design_pct']}%（{dvo*1000:.0f} mV）留余量于课程 ≤3% 建议值；电容放电与 ESR 各占一半")
    esr_max = S.add("C2", "输出电容 ESR 上限", "ESR ≤ (ΔVo/2)/IL,pk", f"{dvo/2:.3f}/{ipk:.3f}", dvo / 2 / ipk * 1e3, "mΩ",
                    "二极管电流在 0 与 IL,pk 间跳变，ESR 纹波 = IL,pk·ESR")
    ic_rms = S.add("C3", "输出电容纹波电流", "IC,rms ≈ Io·√(D/(1−D))", f"{io}×√({d_max_op:.3f}/{1-d_max_op:.3f})",
                   io * math.sqrt(d_max_op / (1 - d_max_op)), "A")
    vc_req = S.add("C4", "输出电容耐压需求", "VC ≥ k·Vovp,hw", f"{k_v}×{v_ovp_hw:.2f}", k_v * v_ovp_hw, "V")
    dvin = vin_nom * ps["vin_ripple_design_pct"] / 100
    dIL_max = max(o["dIL_pp_a"] for o in op.values())
    cin_req = S.add("C5", "输入电容需求", "Cin ≥ ΔIL/(8·fsw·ΔVin)", f"{dIL_max:.3f}/(8×{fsw:.0f}×{dvin:.3f})",
                    dIL_max / (8 * fsw * dvin) * 1e6, "µF", "Boost 输入电流连续，仅需滤除三角纹波")

    # ---------- 7. 占空比限幅 ----------
    d_worst = 1 - vin["min"] * eta_w / vo
    S.add("D6", "最坏占空比需求", "D = 1 − ηworst·Vin,min/Vo", f"1 − {eta_w}×{vin['min']:.3f}/{vo}", d_worst, "")
    dmax = S.add("D7", "占空比上限 Dmax", "Dmax = ceil(Dworst + margin, step)",
                 f"ceil({d_worst:.3f}+{ch['duty']['dmax_margin']}, {ch['duty']['dmax_step']})",
                 ceil_to(d_worst + ch["duty"]["dmax_margin"], ch["duty"]["dmax_step"]), "",
                 "阶段 7 用实际寄生参数核对 Dmax 低于升压比峰值点")
    dmin = ch["duty"]["dmin"]

    # ---------- 8. PWM 与控制时序 ----------
    ftim = ctl["timer_clk_hz"]
    arr = ftim / fsw / (2 if ctl["pwm_alignment"] == "center" else 1)
    S.add("T1", "PWM 周期计数值", "N = ftim/fsw" + ("/2" if ctl["pwm_alignment"] == "center" else ""),
          f"{ftim:.0f}/{fsw:.0f}", arr, "count", f"ARR = {int(round(arr))-1}；分辨率 {math.log2(arr):.1f} bit")
    gvd_nom = vo / (1 - d_nom)
    S.add("T2", "1 LSB 占空比对应输出电压步长", "ΔVo = (Vo/(1−D))/N", f"({vo}/(1−{d_nom:.3f}))/{arr:.0f}",
          gvd_nom / arr * 1e3, "mV", "须远小于 ±5% 精度要求")
    ccr_max = math.floor(dmax * arr)
    rcr = fsw / ctl["f_ctrl_hz"] - 1
    S.add("T3", "控制周期", "Tc = 1/fctrl", f"1/{ctl['f_ctrl_hz']}", 1e6 / ctl["f_ctrl_hz"], "µs",
          f"TIM1 重复计数 RCR = {rcr:.0f}，每 {rcr+1:.0f} 个 PWM 周期更新一次")
    cpu_load = S.add("T4", "控制中断 CPU 占用", "load = cycles·fctrl/fcpu",
                     f"{ctl['isr_cycles_est']}×{ctl['f_ctrl_hz']}/{ctl['cpu_clk_hz']:.0f}",
                     ctl["isr_cycles_est"] * ctl["f_ctrl_hz"] / ctl["cpu_clk_hz"] * 100, "%")
    cm = ch["comms"]
    uart_load = S.add("T5", "调试串口带宽占用", "load = bytes·10·rate/baud",
                      f"{cm['log_frame_bytes']}×10×{cm['log_rate_hz']}/{cm['uart_baud']}",
                      cm["log_frame_bytes"] * 10 * cm["log_rate_hz"] / cm["uart_baud"] * 100, "%")

    # ---------- 9. 采样网络 ----------
    nbits, vref = sen["adc_bits"], sen["adc_vref_v"]
    full = 2 ** nbits - 1
    v_use = vref * sen["adc_usable_frac"]

    def divider(v_fs_target, sid, label):
        rb = sen["divider_rbot_ohm"]
        rt = pick_at_least(rb * (v_fs_target / v_use - 1), E24)
        k = rb / (rt + rb)
        fs_v = vref / k
        rth = rt * rb / (rt + rb)
        cf = pick_nearest(1 / (2 * math.pi * rth * sen["divider_filter_fc_hz"]))
        S.add(sid, f"{label} 分压比", "k = Rb/(Rt+Rb)，Rt 取 E24", f"Rb={rb:.0f}, Rt={rt:.0f}", k, "",
              f"ADC 满量程对应 {fs_v:.2f} V；滤波电容 {cf*1e9:.3g} nF（fc≈{1/(2*math.pi*rth*cf):.0f} Hz）")
        return {"r_top_ohm": rt, "r_bot_ohm": rb, "ratio": k, "fullscale_v": fs_v,
                "filter_c_f": cf, "filter_fc_hz": 1 / (2 * math.pi * rth * cf), "r_thevenin_ohm": rth}

    vdiv = divider(v_ovp_hw * sen["vout_fs_over_ovp_hw"], "S1", "输出电压")
    vdiv["lsb_mv"] = vdiv["fullscale_v"] / full * 1e3
    vdiv["code_at_vset"] = round(vo * vdiv["ratio"] / vref * full)
    vdiv["divider_loss_w"] = vo ** 2 / (vdiv["r_top_ohm"] + vdiv["r_bot_ohm"])
    S.add("S2", "输出电压分辨率", "LSB = VFS/(2^N−1)", f"{vdiv['fullscale_v']:.2f}/{full}", vdiv["lsb_mv"], "mV",
          f"12 V 对应码值 {vdiv['code_at_vset']}")
    idiv = divider(vin["max"] * sen["vin_fs_over_vin_max"], "S3", "输入电压")

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
          f"满量程 {i_fs:.2f} A；0.5 A 时采样电压 {rs*io*1e3:.0f} mV，ADC 输入 {rs*g*io:.2f} V")
    isen = {"location": sen["current_sense_location"], "shunt_ohm": rs, "gain": g, "transimpedance_v_per_a": rs * g,
            "fullscale_a": i_fs, "lsb_ma": i_fs / full * 1e3, "code_at_icc": round(io * rs * g / vref * full),
            "shunt_loss_rated_w": rs * io ** 2, "v_sense_at_icc_mv": rs * io * 1e3}
    S.add("S5", "输出电流分辨率", "LSB = IFS/(2^N−1)", f"{i_fs:.3f}/{full}", isen["lsb_ma"], "mA",
          f"0.5 A 对应码值 {isen['code_at_icc']}")

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
                   "action": "片内比较器 + DAC 阈值 → TIM1 刹车，逐周期硬件封锁 PWM"},
        "ocp_sw": {"value": i_ocp_sw, "unit": "A", "adc_code": icode(i_ocp_sw), "delay_ms": pro["ocp_sw_delay_ms"],
                   "action": "持续超限后关断 PWM 并锁存（CC 环正常时不会触发）"},
        "ocp_hw": {"value": i_ocp_hw, "unit": "A", "adc_code": icode(i_ocp_hw),
                   "action": "片内比较器 → TIM1 刹车"},
        "uvlo": {"off_v": pro["uvlo_off_v"], "on_v": pro["uvlo_on_v"],
                 "adc_code_off": vincode(pro["uvlo_off_v"]), "adc_code_on": vincode(pro["uvlo_on_v"]),
                 "action": "输入欠压停机，回差恢复"},
        "duty_limit": {"dmin": dmin, "dmax": dmax, "ccr_max": ccr_max, "action": "PI 输出限幅 + 定时器比较值钳位"},
        "soft_start": {"ramp_ms": ctl["soft_start_ms"],
                       "action": "参考电压从当前 Vout 以斜坡升至 12 V，避免启动过冲与浪涌"},
    }
    S.add("PR1", "软件过压阈值", "Vovp,sw = Vo·(1+x%)", f"{vo}×(1+{pro['ovp_sw_pct']}%)", v_ovp_sw, "V",
          f"ADC 码值 {prot['ovp_sw']['adc_code']}")
    S.add("PR2", "硬件过压阈值", "Vovp,hw = Vo·(1+y%)", f"{vo}×(1+{pro['ovp_hw_pct']}%)", v_ovp_hw, "V",
          f"比较器参考码值 {prot['ovp_hw']['adc_code']}")
    S.add("PR3", "软件过流阈值", "Iocp,sw = Icc·(1+z%)", f"{io}×(1+{pro['ocp_sw_pct']}%)", i_ocp_sw, "A")
    S.add("PR4", "硬件过流阈值", "设定值", f"{i_ocp_hw}", i_ocp_hw, "A",
          f"此时电感电流约 {i_ocp_hw/(1-op['min']['d_est']):.2f} A（瞬态），提示电感饱和裕量")

    # ---------- 11. CC 可调范围 / 短路边界 ----------
    vo_cc_min = vin["max"] + vf + pro["cc_vout_min_margin_v"]
    S.add("CC1", "CC 模式最低可调输出电压", "Vo,cc,min = Vin,max + VF + margin",
          f"{vin['max']:.2f}+{vf}+{pro['cc_vout_min_margin_v']}", vo_cc_min, "V",
          "Boost 关断时输入经 L、D 直通输出，Vo 不可能低于 Vin−VF，故无法对短路限流")
    r_cc_min = S.add("CC2", "CC 模式最小负载电阻", "Rmin = Vo,cc,min/Icc", f"{vo_cc_min:.2f}/{io}", vo_cc_min / io, "Ω",
                     f"CR 测试 CC 段负载范围 {vo_cc_min/io:.1f}–{rl:.0f} Ω；更低阻值或短路依靠输入保险丝/电源限流")

    # ---------- 12. 辅助电源 ----------
    i3v3 = aux["mcu_run_ma"] + aux["csa_ma"] + aux["led_count"] * aux["led_ma"] + aux["misc_ma"]
    S.add("A1", "3.3 V 负载电流", "I = IMCU + ICSA + n·ILED + Imisc",
          f"{aux['mcu_run_ma']}+{aux['csa_ma']}+{aux['led_count']}×{aux['led_ma']}+{aux['misc_ma']}", i3v3, "mA")
    p_ldo = S.add("A2", "LDO 功耗", "P = (Vin,max − 3.3)·I", f"({vin['max']:.2f}−3.3)×{i3v3:.1f}m",
                  (vin["max"] - 3.3) * i3v3, "mW")
    S.add("A3", "LDO 允许压差", "Vdo ≤ Vin,min − 3.3", f"{vin['min']:.2f}−3.3", vin["min"] - 3.3, "V",
          "选 LDO 时其满载压差须小于该值并留裕量")
    p_aux = (vin["nom"] * i3v3 + vin["nom"] * aux["gate_driver_iq_ma"]) / 1000

    return {
        "schema": "boost-calc/1",
        "revision": ch.get("revision"),
        "inputs": {"vin": vin, "vout_v": vo, "iout_cc_a": io, "pout_w": po, "fsw_hz": fsw,
                   "eta_assumed": eta, "eta_worst": eta_w, "diode_vf_assumed_v": vf},
        "operating_points": {k: {kk: r3(vv) for kk, vv in o.items()} for k, o in op.items()},
        "inductor": {"l_required_uh": r3(l_req), "l_selected_uh": l_sel_uh, "l_min_effective_uh": r3(l_min * 1e6),
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
    out = calc(load_json(p["requirements"]), load_json(p["choices"]))
    save_json(p["calc"], out)
    print(f"calc.json written: L={out['inductor']['l_selected_uh']} µH, Isat≥{out['inductor']['isat_required_a']} A, "
          f"Cout,eff≥{out['output_cap']['c_eff_required_uf']} µF, Dmax={out['duty']['dmax']}, "
          f"Rs={out['sensing']['iout']['shunt_ohm']} Ω × G{out['sensing']['iout']['gain']}")


if __name__ == "__main__":
    main()
