"""阶段 7a — 用入选器件复核：工作点、损耗分解、效率、结温、输出纹波、增益峰值与上电浪涌.

与阶段 5 的初算不同，这里用含寄生参数的平均模型求实际占空比与电流，
并用 parts_db 中有出处的器件参数逐项计算损耗。若效率与 calc 假设 η 偏差 > 3%，
提示更新 design_choices.power_stage.eta_assumed 后重跑（反应式修正闭环）。

用法: python loss_thermal.py <workdir> [--db parts_db.json]
"""

import argparse
import cmath
import math

from _common import KNOWLEDGE_DIR, Steps, load_json, param, r3, save_json, workdir_paths
from stage_params import build, selected


def ripple_harmonic(d, ipk, ival, fsw, caps, R, n_harm=60, N=1024):
    """Output ripple Vpp: Fourier series of the diode current through the parallel C-ESR network."""
    io_avg = (1 - d) * (ipk + ival) / 2
    wave = []
    for k in range(N):
        t = k / N
        wave.append(0.0 if t < d else ipk - (ipk - ival) * (t - d) / (1 - d))
    coef = []
    for n in range(1, n_harm + 1):
        c = sum(w * cmath.exp(-2j * math.pi * n * k / N) for k, w in enumerate(wave)) / N
        w = 2 * math.pi * n * fsw
        y = sum(1 / (r + 1 / (1j * w * C)) for C, r in caps) + 1 / R
        coef.append(c / y)
    v = [sum(2 * (cz * cmath.exp(2j * math.pi * n * k / N)).real for n, cz in enumerate(coef, 1)) for k in range(0, N, 4)]
    c_tot = sum(C for C, _ in caps)
    return max(v) - min(v), io_avg * d / (fsw * c_tot)


def run(calc, ch, sel, db):
    S = Steps()
    model, minfo = build(sel, db, calc, ch)
    lc = ch.get("losses", {})
    ta = lc.get("ambient_c", 40)
    rg_ext = lc.get("gate_resistor_ohm", 10)
    v_plateau = lc.get("miller_plateau_v", 2.5)
    core_frac = lc.get("core_loss_frac_of_copper", 0.25)
    vo, io, fsw = calc["inputs"]["vout_v"], calc["inputs"]["iout_cc_a"], calc["inputs"]["fsw_hz"]
    po = vo * io
    q, _ = selected(sel, db, "mosfet")
    dio, _ = selected(sel, db, "diode")
    drv, _ = selected(sel, db, "gate_driver")
    pm, _ = selected(sel, db, "pmos")
    fuse, _ = selected(sel, db, "fuse")
    ind, _ = selected(sel, db, "inductor")
    sens = calc["sensing"]

    def p_or(part, name, default, fields=("typ", "max")):
        v = param(part, name, fields) if part else None
        return (v if v is not None else default), (v is None)

    qg, qg_def = p_or(q, "qg_nc", 8.0)
    qgd, qgd_def = p_or(q, "qgd_nc", qg * 0.3)
    qgs, qgs_def = p_or(q, "qgs_nc", qg * 0.2)
    coss, coss_def = p_or(q, "coss_pf", 80.0)
    rg_int, _ = p_or(q, "rg_ohm", 1.5)
    rth_q, rthq_def = p_or(q, "rth_ja_cw", 125.0, ("typ", "max"))
    cj, _ = p_or(dio, "cj_pf", 150.0)
    rth_d, rthd_def = p_or(dio, "rth_ja_cw", 80.0, ("typ", "max"))
    i_src, _ = p_or(drv, "i_source_a", 2.0)
    i_snk, _ = p_or(drv, "i_sink_a", 2.0)
    drv_iq, _ = p_or(drv, "iq_ma", 0.1)
    rds_p = (p_or(pm, "rds_on_mohm_vgs4v5", 60.0, ("max", "typ"))[0] * 1e-3 * minfo["rds_hot_factor"]) if pm else 0.0
    rth_p, _ = p_or(pm, "rth_ja_cw", 125.0, ("typ", "max"))
    r_fuse = (p_or(fuse, "r_mohm", 50.0, ("max", "typ"))[0] * 1e-3) if fuse else 0.0
    defaults_used = [n for n, f in (("Qg", qg_def), ("Qgd", qgd_def), ("Qgs", qgs_def), ("Coss", coss_def),
                                    ("RθJA(MOSFET)", rthq_def), ("RθJA(diode)", rthd_def)) if f]

    cases = {}
    for k, vin in calc["inputs"]["vin"].items():
        dc = model.solve_dc(vin, vo, io)
        if dc is None:
            cases[k] = {"error": "额定点不可达"}
            continue
        d, il = dc
        dI = vin * d / (fsw * model.L)
        il_rms2 = il ** 2 + dI ** 2 / 12
        ipk, ival = il + dI / 2, il - dI / 2
        vsw = vo + model.vf0 + model.rd * ipk
        vdrv = vin
        r_on = vdrv / i_src + rg_ext + rg_int
        r_off = vdrv / i_snk + rg_ext + rg_int
        qsw = qgd + qgs / 2
        t_r = qsw / ((vdrv - v_plateau) / r_on) * 1e-9
        t_f = qsw / (v_plateau / r_off) * 1e-9
        L = {}
        L["MOSFET 导通"] = d * il_rms2 * model.rds
        L["MOSFET 开关"] = 0.5 * vsw * (ival * t_r + ipk * t_f) * fsw
        L["MOSFET Coss"] = 0.5 * coss * 1e-12 * vsw ** 2 * fsw
        L["二极管导通"] = model.vf0 * io + model.rd * (1 - d) * il_rms2
        L["二极管结电容"] = 0.5 * cj * 1e-12 * vsw ** 2 * fsw
        L["电感铜损"] = il_rms2 * model.dcr
        L["电感磁损(估)"] = core_frac * L["电感铜损"]
        esr_c = model.caps[0][1]
        L["输出电容 ESR"] = (io * math.sqrt(d / (1 - d))) ** 2 * esr_c
        L["采样电阻"] = io ** 2 * sens["iout"]["shunt_ohm"]
        L["分压电阻"] = sens["vout"]["divider_loss_w"] + vin ** 2 / (sens["vin"]["r_top_ohm"] + sens["vin"]["r_bot_ohm"])
        L["防反接 PMOS"] = il_rms2 * rds_p
        L["保险丝"] = il ** 2 * r_fuse
        L["栅极驱动"] = qg * 1e-9 * vdrv * fsw + vdrv * drv_iq * 1e-3
        L["3.3V 辅助电源"] = vin * calc["aux"]["i_3v3_ma"] * 1e-3
        p_tot = sum(L.values())
        p_aux = L["栅极驱动"] + L["3.3V 辅助电源"]
        eta = po / (po + p_tot)
        eta_pwr = po / (po + p_tot - p_aux)
        tj = {"MOSFET": ta + (L["MOSFET 导通"] + L["MOSFET 开关"] + L["MOSFET Coss"]) * rth_q,
              "二极管": ta + (L["二极管导通"] + L["二极管结电容"]) * rth_d}
        if pm:
            tj["防反接 PMOS"] = ta + L["防反接 PMOS"] * rth_p
        cases[k] = {"vin_v": vin, "D": r3(d), "IL_avg_A": r3(il), "IL_pk_A": r3(ipk), "dIL_A": r3(dI),
                    "IL_rms_A": r3(math.sqrt(il_rms2)), "t_rise_ns": r3(t_r * 1e9), "t_fall_ns": r3(t_f * 1e9),
                    "losses_W": {n: r3(v) for n, v in L.items()}, "p_total_W": r3(p_tot),
                    "eta_total": r3(eta), "eta_power_stage": r3(eta_pwr), "iin_avg_A": r3((po + p_tot) / vin),
                    "tj_C": {n: r3(v, 3) for n, v in tj.items()}}

    nom = cases["nom"]
    S.add("E1", "额定点总损耗 (Vin_nom)", "ΣP", " + ".join(f"{n}" for n in nom["losses_W"]), nom["p_total_W"], "W")
    S.add("E2", "额定点整机效率 (含控制电路)", "η = Po/(Po+ΣP)", f"{po}/({po}+{nom['p_total_W']})", nom["eta_total"] * 100, "%")
    S.add("E3", "功率级效率 (不含控制电路)", "η = Po/(Po+ΣP−Paux)", "", nom["eta_power_stage"] * 100, "%")
    worst = min((c for c in cases.values() if "eta_total" in c), key=lambda c: c["eta_total"])
    S.add("E4", "最差效率 (Vin_min)", "同上", f"Vin={worst['vin_v']:.2f} V", worst["eta_total"] * 100, "%")

    # 输出纹波（实际电容网络，谐波叠加法）
    d_w, ipk_w, ival_w = worst["D"], worst["IL_pk_A"], worst["IL_pk_A"] - worst["dIL_A"]
    c_tot = sum(C for C, _ in model.caps)
    vpp, dv_simple = ripple_harmonic(d_w, ipk_w, ival_w, fsw, model.caps, vo / io)
    S.add("R1", "输出纹波：仅电容放电（简化公式对照）", "ΔVc = Io·D/(fsw·Ctot)",
          f"{io}×{d_w}/({fsw:.0f}×{c_tot*1e6:.1f}µ)", dv_simple * 1e3, "mV", "把电解电容当理想电容，偏乐观")
    S.add("R2", "输出纹波：谐波叠加（计 ESR 与支路分流）", "v(t)=Σ In·Zout(n·fsw)",
          f"二极管电流梯形波 {ipk_w:.2f}→{ival_w:.2f} A，D={d_w}", vpp * 1e3, "mV",
          "Zout = 各电容支路 (ESR + 1/jωC) 与负载并联；60 次谐波")
    ripple_pct = S.add("R3", "输出纹波估算（峰峰值/直流）", "Vpp/Vo", f"{vpp*1e3:.1f}m/{vo}", vpp / vo * 100, "%",
                       "未计 ESL 与开关尖峰（取决于 PCB 布局与测量方法，建议 20 MHz 带宽限制、弹簧地针测量）")
    dv_c, dv_esr = dv_simple, vpp

    # 增益峰值
    R = vo / io
    dpk, vpk = model.gain_peak(calc["inputs"]["vin"]["min"], R)
    S.add("G1", "升压比峰值点（Vin_min, 满载）", "max Vo(D)", f"D={dpk:.3f}", vpk, "V",
          "Dmax 必须低于此 D，否则过载时增大占空比反而降低输出，环路失去单调性")

    # 上电浪涌（输出电容经 L、D 直接充电）
    z0 = math.sqrt(model.L / c_tot)
    rs = model.dcr + model.rd + r_fuse + rds_p
    zeta = rs / (2 * z0)
    vin_max = calc["inputs"]["vin"]["max"]
    i_inrush = (vin_max - model.vf0) / z0 * math.exp(-zeta * math.pi / 2 / math.sqrt(max(1 - zeta ** 2, 1e-6))) if zeta < 1 else (vin_max - model.vf0) / rs
    S.add("U1", "热插拔上电浪涌峰值（估）", "I ≈ (Vin−VF)/√(L/C)·e^(−ζπ/2/√(1−ζ²))",
          f"Z0={z0:.2f} Ω, ζ={zeta:.3f}", i_inrush, "A", "PWM 未启动时输出电容经 L、D 被动充电；建议 PMOS 栅极 RC 缓启动或实验电源限流")

    i2t = i_inrush ** 2 * math.pi * math.sqrt(model.L * c_tot) / 2  # 半正弦近似：I²·(π√LC)/2
    fuse_i2t = param(fuse, "melting_i2t_a2s") if fuse else None
    S.add("U2", "上电浪涌 I²t", "I²t ≈ Ipk²·π√(LC)/2", f"{i_inrush:.2f}²×π√({model.L*1e6:.0f}µ×{c_tot*1e6:.0f}µ)/2", i2t, "A²s",
          f"保险丝熔断 I²t = {fuse_i2t} A²s" if fuse_i2t else "未选保险丝或无 I²t 数据")

    eta_ass = ch["power_stage"]["eta_assumed"]
    iterate = abs(nom["eta_total"] - eta_ass) > 0.03
    return {
        "schema": "boost-losses/1", "revision": ch.get("revision"), "model": minfo,
        "assumptions": {"ambient_c": ta, "gate_resistor_ohm": rg_ext, "miller_plateau_v": v_plateau,
                        "core_loss_frac_of_copper": core_frac, "defaults_used_for_missing_data": defaults_used},
        "cases": cases,
        "ripple": {"vpp_simple_mv": r3(dv_c * 1e3), "vpp_harmonic_mv": r3(dv_esr * 1e3), "ripple_pct": r3(ripple_pct)},
        "gain_peak": {"d_peak": r3(dpk), "vo_peak_v": r3(vpk)},
        "inrush": {"z0_ohm": r3(z0), "zeta": r3(zeta), "i_peak_a": r3(i_inrush), "i2t_a2s": r3(i2t),
                   "fuse_melting_i2t_a2s": fuse_i2t},
        "eta_consistency": {"eta_assumed": eta_ass, "eta_model_nom": nom["eta_total"], "needs_iteration": iterate,
                            "advice": (f"把 design_choices.power_stage.eta_assumed 改为 {round(nom['eta_total'], 2)} 后重跑 calc→select→loss"
                                       if iterate else "假设与复核一致（偏差 ≤ 3%）")},
        "steps": S.items,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir")
    ap.add_argument("--db", default=str(KNOWLEDGE_DIR / "parts_db.json"))
    a = ap.parse_args()
    p = workdir_paths(a.workdir)
    res = run(load_json(p["calc"]), load_json(p["choices"]), load_json(p["selection"]), load_json(a.db))
    save_json(p["losses"], res)
    n = res["cases"]["nom"]
    print(f"η(nom)={n['eta_total']*100:.1f}% (功率级 {n['eta_power_stage']*100:.1f}%), ΣP={n['p_total_W']} W, "
          f"Tj={n['tj_C']}, 纹波≈{res['ripple']['ripple_pct']}%, 浪涌≈{res['inrush']['i_peak_a']} A")
    print("η 迭代：", res["eta_consistency"]["advice"])


if __name__ == "__main__":
    main()
