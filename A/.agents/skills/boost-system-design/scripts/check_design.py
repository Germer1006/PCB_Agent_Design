"""阶段 8 — 系统设计规则检查（System Design Rule Check, SDRC）.

类比 PCB 的 DRC：把需求、计算、选型、复核结果放在一起做一致性与裕量检查，
每条规则给出 PASS / WARN / FAIL 及数值依据。FAIL 必须回到对应阶段修改后重跑。

用法: python check_design.py <workdir> [--db parts_db.json]
"""

import argparse

from _common import KNOWLEDGE_DIR, Checks, load_json, param, save_json, workdir_paths
from stage_params import selected


def run(req, ch, calc, sel, loss, loop, db):
    K = Checks()
    vo, io = calc["inputs"]["vout_v"], calc["inputs"]["iout_cc_a"]
    vin = calc["inputs"]["vin"]
    prot = calc["protection"]

    # --- 需求一致性 ---
    K.add("SR01", "额定功率与 Vo·Io 一致", abs(vo * io - req["pout"]["rated_w"]) / req["pout"]["rated_w"] <= 0.05,
          f"{vo}×{io} = {vo*io} W vs {req['pout']['rated_w']} W", ref="REQ-PWR")
    K.add("SR02", "Vin,max < Vo（Boost 适用）", vin["max"] < vo, f"{vin['max']:.2f} V < {vo} V", ref="DEC-TOPO")
    lo, hi = req["fsw"]["range_hz"]
    K.add("SR03", "开关频率在课程范围内", lo <= calc["inputs"]["fsw_hz"] <= hi, f"{calc['inputs']['fsw_hz']} Hz ∈ [{lo},{hi}]",
          ref="REQ-FSW")
    for r in req["protection"]["required"] + req["protection"]["suggested"]:
        key = {"OVP": "ovp_sw", "OCP": "ocp_sw", "duty_limit": "duty_limit", "soft_start": "soft_start"}[r]
        K.add(f"SR04-{r}", f"保护功能 {r} 已定义阈值与动作", key in prot, prot.get(key, {}).get("action", "缺失"), ref="REQ-PROT")
    K.add("SR05", "MCU 为 32 位 STM32", sel["categories"]["mcu"]["selected"] is not None and
          "STM32" in (selected(sel, db, "mcu")[0] or {}).get("mpn", ""), str(sel["categories"]["mcu"]["selected"]), ref="REQ-MCU")

    # --- 选型完整性 ---
    for cat, v in sel["categories"].items():
        K.add(f"SR10-{cat}", f"{cat} 有通过硬性规则的入选器件", v["selected"] is not None, v["reason"], ref="SEL")
    for cat, v in sel["categories"].items():
        part, _ = selected(sel, db, cat)
        if part:
            K.add(f"SR11-{cat}", f"{cat} 立创商城编号已核实", bool(part.get("lcsc")), f"{part.get('mpn')} → {part.get('lcsc')}",
                  warn_only=True, ref="SOURCING")

    # --- 占空比 / 增益峰 ---
    gp = loss["gain_peak"]
    K.add("SR20", "Dmax > 实际最坏占空比", prot["duty_limit"]["dmax"] > max(c["D"] for c in loss["cases"].values() if "D" in c),
          f"Dmax={prot['duty_limit']['dmax']} vs D,max={max(c['D'] for c in loss['cases'].values() if 'D' in c)}", ref="CTRL-DUTY")
    K.add("SR21", "Dmax < 升压比峰值点占空比", prot["duty_limit"]["dmax"] < gp["d_peak"],
          f"Dmax={prot['duty_limit']['dmax']} < D_peak={gp['d_peak']}（峰值 Vo={gp['vo_peak_v']} V）", ref="CTRL-DUTY")
    vpk_dmax_ok = gp["vo_peak_v"] > prot["ovp_hw"]["value"]
    K.add("SR22", "失控时可达电压 > 硬件 OVP（OVP 有意义且必须存在）", vpk_dmax_ok,
          f"峰值 {gp['vo_peak_v']} V > {prot['ovp_hw']['value']:.1f} V → 必须依赖 OVP/TVS 防护", warn_only=True, ref="PROT")

    # --- 保护阈值次序 ---
    tvs, _ = selected(sel, db, "tvs")
    cb, _ = selected(sel, db, "cout_bulk")
    cc, _ = selected(sel, db, "cout_ceramic")
    q, _ = selected(sel, db, "mosfet")
    vbr = param(tvs, "vbr_min_v", ("min", "typ")) if tvs else None
    vcl = param(tvs, "vc_v", ("max", "typ")) if tvs else None
    v_caps = min(x for x in (param(cb, "voltage_v") if cb else None, param(cc, "voltage_v") if cc else None) if x)
    vds = param(q, "vds_max_v") if q else None
    chain = [vo, prot["ovp_sw"]["value"], prot["ovp_hw"]["value"]] + ([vbr] if vbr else [])
    K.add("SR30", "阈值次序 Vo < OVPsw < OVPhw < TVS VBR,min", all(a < b for a, b in zip(chain, chain[1:])),
          " < ".join(f"{v:.1f}" for v in chain), ref="PROT")
    if vcl:
        K.add("SR31", "TVS 钳位电压 ≤ MOSFET Vds 额定", vds is not None and vcl <= vds, f"VC={vcl} V vs Vds={vds} V",
              warn_only=True, ref="PROT")
        K.add("SR32", "TVS 钳位电压 ≤ 输出电容耐压", vcl <= v_caps, f"VC={vcl} V vs {v_caps} V（VC 为 IPP 大电流下的值，"
              "实际故障电流小得多，通常可接受）", warn_only=True, ref="PROT")
    K.add("SR33", "OCP 次序 Icc < OCPsw < OCPhw", io < prot["ocp_sw"]["value"] < prot["ocp_hw"]["value"],
          f"{io} < {prot['ocp_sw']['value']} < {prot['ocp_hw']['value']} A", ref="PROT")
    ind, _ = selected(sel, db, "inductor")
    il_ocp = prot["ocp_hw"]["value"] / (1 - max(c["D"] for c in loss["cases"].values() if "D" in c))
    isat = param(ind, "isat_a", ("min", "typ")) if ind else None
    K.add("SR34", "硬件 OCP 动作前电感不深度饱和", isat is not None and isat >= il_ocp * 0.9,
          f"OCPhw 对应 IL≈{il_ocp:.2f} A vs Isat={isat} A（瞬态，比较器 μs 级关断）", warn_only=True, ref="PROT")

    # --- 采样 ---
    s = calc["sensing"]
    vref, full = s["adc_vref_v"], 2 ** s["adc_bits"] - 1
    K.add("SR40", "Vout 采样在硬件 OVP 处不超量程", prot["ovp_hw"]["adc_code"] < 0.97 * full,
          f"code {prot['ovp_hw']['adc_code']} / {full}", ref="IF-ADC")
    K.add("SR41", "Iout 采样在硬件 OCP 处不超量程", prot["ocp_hw"]["adc_code"] < 0.97 * full,
          f"code {prot['ocp_hw']['adc_code']} / {full}", ref="IF-ADC")
    K.add("SR42", "Vout 分辨率 ≤ 0.1% Vo", s["vout"]["lsb_mv"] / 1000 <= 0.001 * vo, f"{s['vout']['lsb_mv']} mV", ref="IF-ADC")
    K.add("SR43", "Iout 分辨率 ≤ 0.2% Icc", s["iout"]["lsb_ma"] / 1000 <= 0.002 * io, f"{s['iout']['lsb_ma']} mA", ref="IF-ADC")
    csa, _ = selected(sel, db, "csa")
    if csa:
        vos = param(csa, "vos_uv", ("max", "typ")) or 0
        ge = param(csa, "gain_error_pct", ("max", "typ")) or 0
        err = vos * 1e-6 / (s["iout"]["shunt_ohm"] * io) * 100 + ge + 1.0  # + 分流电阻 1%
        K.add("SR44", "电流测量最坏误差 ≤ 稳态精度要求的一半", err <= req["iout"]["steady_err_pct"] / 2,
              f"失调 + 增益误差 + 分流 1% ≈ {err:.2f}% vs ≤{req['iout']['steady_err_pct']/2}%（可两点标定进一步降低）",
              warn_only=True, ref="REQ-ACC")
    verr = 2 * 1.0 + 0.5  # 两只 1% 电阻 + ADC/基准
    K.add("SR45", "电压测量最坏误差 ≤ 稳态精度要求的一半", verr <= req["vout"]["steady_err_pct"] / 2,
          f"≈{verr}%（1% 电阻×2 + ADC 0.5%）", warn_only=True, ref="REQ-ACC")

    # --- 效率 / 热 / 纹波 ---
    eta = loss["cases"]["nom"]["eta_total"] * 100
    K.add("SR50", "额定点效率估算 ≥ 课程目标 + 5% 裕量", eta >= req["efficiency"]["target_pct"] + 5,
          f"{eta:.1f}% vs {req['efficiency']['target_pct']}%（+5% 留给未建模损耗）", ref="REQ-EFF")
    for c in loss["cases"].values():
        for n, t in c.get("tj_C", {}).items():
            if c is loss["cases"]["min"]:
                K.add(f"SR51-{n}", f"{n} 结温 < 100 °C（Vin_min, Ta={loss['assumptions']['ambient_c']}°C）", t < 100, f"{t} °C",
                      ref="THERM")
    rp = loss["ripple"]["ripple_pct"]
    K.add("SR52", "输出纹波估算 ≤ 建议值 3%", rp <= req["ripple"]["target_pct"], f"{rp}%（不含开关尖峰）", ref="REQ-RIP")
    K.add("SR53", "上电浪涌已评估并有对策", loss["inrush"]["i_peak_a"] < 3 or ch["protection"].get("input_reverse_protection") == "pmos",
          f"{loss['inrush']['i_peak_a']} A；对策：PMOS 缓启动/电源限流", warn_only=True, ref="PROT")

    fi = loss["inrush"].get("fuse_melting_i2t_a2s")
    K.add("SR54", "保险丝不因上电浪涌熔断（熔断 I²t ≥ 10×浪涌 I²t）", fi is not None and fi >= 10 * loss["inrush"]["i2t_a2s"],
          f"熔断 {fi} A²s vs 浪涌 {loss['inrush']['i2t_a2s']} A²s", warn_only=fi is None, ref="PROT")

    # --- 控制 ---
    for m in ("CV", "CC"):
        r = loop[m]
        K.add(f"SR60-{m}", f"{m} 环路在所有工况角满足 PM≥{ch['control']['pm_min_deg']}°, GM≥{ch['control']['gm_min_db']} dB",
              r["ok"], f"Kp={r.get('Kp')}, Ki={r.get('Ki')}, 最差 fc={r.get('fc_min_Hz')} Hz", ref="CTRL")
    K.add("SR61", "PWM 分辨率 ≥ 10 bit", calc["pwm"]["resolution_bits"] >= 10, f"{calc['pwm']['resolution_bits']} bit", ref="CTRL")
    K.add("SR62", "控制中断 CPU 占用 < 50%", calc["timing"]["cpu_load_pct"] < 50, f"{calc['timing']['cpu_load_pct']}%", ref="FW")
    K.add("SR63", "日志串口占用 < 50%", calc["timing"]["uart_load_pct"] < 50, f"{calc['timing']['uart_load_pct']}%", ref="FW")
    K.add("SR64", "CC 可调范围与短路边界已说明", "short_circuit" in calc["cc_range"],
          f"CR {calc['cc_range']['r_min_ohm']}–{calc['cc_range']['r_max_ohm']} Ω", ref="REQ-CC")

    # --- 供电 ---
    ldo, _ = selected(sel, db, "ldo")
    drv, _ = selected(sel, db, "gate_driver")
    if ldo:
        vdo = param(ldo, "dropout_mv", ("max", "typ"))
        K.add("SR70", "LDO 压差余量 ≥ 200 mV @Vin,min", vdo is not None and (vin["min"] - 3.3) * 1000 - vdo >= 200,
              f"余量 {(vin['min']-3.3)*1000 - (vdo or 0):.0f} mV", ref="PWR-AUX")
    if drv:
        uv = param(drv, "uvlo_on_v", ("max", "typ"))
        vmin = param(drv, "vdd_min_v", ("min", "typ"))
        pm, _ = selected(sel, db, "pmos")
        fu, _ = selected(sel, db, "fuse")
        r_path = ((param(pm, "rds_on_mohm_vgs4v5", ("max", "typ")) or 0) * loss["model"]["rds_hot_factor"]
                  + (param(fu, "r_mohm", ("max", "typ")) or 0)) / 1000
        iin = loss["cases"]["min"]["iin_avg_A"]
        vdd = vin["min"] - iin * r_path
        need = max(x for x in (uv, vmin) if x is not None) if (uv or vmin) else None
        K.add("SR71", "驱动器最低供电 > max(UVLO 开启最大值, 保证工作下限) + 0.1 V", need is not None and vdd >= need + 0.1,
              f"VDD,min = {vin['min']:.2f} − {iin:.2f} A×{r_path*1000:.0f} mΩ = {vdd:.2f} V vs {need} V", ref="PWR-AUX")
    K.add("SR72", "η 假设与复核一致", not loss["eta_consistency"]["needs_iteration"], loss["eta_consistency"]["advice"],
          warn_only=True, ref="ITER")

    summ = K.summary()
    status = "ready_for_downstream" if summ["FAIL"] == 0 else "needs_rework"
    return {"schema": "boost-sdrc/1", "revision": ch.get("revision"), "summary": summ, "status": status, "checks": K.items}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir")
    ap.add_argument("--db", default=str(KNOWLEDGE_DIR / "parts_db.json"))
    a = ap.parse_args()
    p = workdir_paths(a.workdir)
    res = run(*(load_json(p[k]) for k in ("requirements", "choices", "calc", "selection", "losses", "loop")), load_json(a.db))
    save_json(p["check"], res)
    print(f"SDRC: {res['summary']} → {res['status']}")
    for c in res["checks"]:
        if c["verdict"] != "PASS":
            print(f"  [{c['verdict']}] {c['id']} {c['rule']}: {c['detail']}")


if __name__ == "__main__":
    main()
