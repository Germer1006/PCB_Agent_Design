"""阶段 7b — CC/CV 数字 PI 初值设计与稳定裕度校核.

模型：含寄生参数的 Boost CCM 平均小信号模型（boost_model.py）
      × 采样 RC 滤波 × 一拍计算延迟 z⁻¹ × 零阶保持（控制周期 Tc）
控制器：离散 PI  u[k] = Kp·e[k] + I[k],  I[k] = I[k−1] + Ki·Tc·e[k]
        e 以物理量计（V 或 A），u 为占空比；固件中先把 ADC 码换算为物理量再进 PI。
方法：在全部工况角（Vin 最小/标称/最大 × 多种负载）上网格搜索 (Kp, Ki)，
      要求每个角：单一增益穿越、穿越前相位不低于 −180°、PM ≥ pm_min、GM ≥ gm_min；
      在满足约束的组合中取“最差角穿越频率最高”者。
局限：CCM 模型，DCM 轻载不覆盖；离散化采用低频近似；结果是联调初值，须仿真/实测确认。

用法: python loop_design.py <workdir> [--db parts_db.json]
输出 loop.json、bode_CV.csv、bode_CC.csv、bode_loops.svg
"""

import argparse
import cmath
import math
from pathlib import Path

from _common import KNOWLEDGE_DIR, load_json, r3, save_json, workdir_paths
from boost_model import Boost, logspace, unwrap_deg
from stage_params import build, variants, selected
from _common import param
from svgplot import bode_svg


def corners(calc):
    vin = calc["inputs"]["vin"]
    vo, io = calc["inputs"]["vout_v"], calc["inputs"]["iout_cc_a"]
    rl = vo / io
    cv, cc = [], []
    for k in ("min", "nom", "max"):
        for name, I0, Rac in (("满载 R=24Ω", 0.0, rl), ("半载 R=48Ω", 0.0, 2 * rl),
                              ("轻载 R=100Ω", 0.0, 100.0),
                              (f"电子负载 CC {0.2*io:g}A", 0.2*io, None),
                              (f"电子负载 CC {0.5*io:g}A", 0.5*io, None),
                              (f"电子负载 CC {0.9*io:g}A", 0.9*io, None),
                              (f"电子负载 CC {io:g}A（交界）", io, None)):
            cv.append({"vin_key": k, "vin": vin[k], "load": name.replace("24", f"{rl:.0f}").replace("48", f"{2*rl:.0f}"),
                       "vo": vo, "I0": I0, "Rac": Rac})
        for R in (calc["cc_range"]["r_min_ohm"], (calc["cc_range"]["r_min_ohm"] + rl) / 2, rl):
            cc.append({"vin_key": k, "vin": vin[k], "load": f"CR R={R:.1f}Ω", "vo": io * R, "I0": 0.0, "Rac": R})
    return cv, cc


def margins(freqs, Lw):
    mag = [abs(z) for z in Lw]
    ph = unwrap_deg(Lw)
    # anchor unwrapped phase so that low-frequency phase is near −90° (integrator)
    shift = round((ph[0] + 90) / 360) * 360
    ph = [p - shift for p in ph]
    xs = [i for i in range(len(mag) - 1) if (mag[i] - 1) * (mag[i + 1] - 1) < 0]
    if len(xs) != 1 or mag[xs[0]] < 1:
        return None
    i = xs[0]
    t = math.log(mag[i]) / (math.log(mag[i]) - math.log(mag[i + 1]))
    fc = freqs[i] * (freqs[i + 1] / freqs[i]) ** t
    pc = ph[i] + (ph[i + 1] - ph[i]) * t
    if any(p <= -180 for p in ph[: i + 1]):
        return None
    pm = 180 + pc
    gm = math.inf
    for j in range(i, len(ph) - 1):
        for lvl in (-180, -540):
            if (ph[j] - lvl) * (ph[j + 1] - lvl) <= 0:
                t = (lvl-ph[j])/(ph[j+1]-ph[j]) if ph[j+1] != ph[j] else 0
                mc = math.log(max(mag[j],1e-30)) + t*(math.log(max(mag[j+1],1e-30))-math.log(max(mag[j],1e-30)))
                gm = min(gm, -20/math.log(10)*mc)
    gm = min(gm, -20 * math.log10(mag[-1]))  # Nyquist-edge guard
    return fc, pm, gm


def ctrl_resp(f, Tc, ffilt):
    w = 2 * math.pi * f
    z1 = cmath.exp(-1j * w * Tc)
    zoh = (1 - z1) / (1j * w * Tc)
    poles = ffilt if isinstance(ffilt, (list, tuple)) else [ffilt]
    filt = 1.0
    for pole in poles:
        filt /= 1 + 1j*f/pole
    return z1, zoh * z1 * filt  # (z^-1 for PI, plant-side dynamics)


def design_mode(model, cs, mode, Tc, ffilt, pm_min, gm_min, freqs):
    plants, info = [], []
    excluded = []
    for c in cs:
        model = c['_model']
        clean = {k:v for k,v in c.items() if k != '_model'}
        io_tot = c["I0"] + (c["vo"] / c["Rac"] if c["Rac"] else 0)
        rs = c['_shunt']
        vb = c['vo'] + io_tot*rs
        rac = c['Rac'] + rs if c['Rac'] else None
        dc = model.solve_dc(c["vin"], vb, io_tot)
        if dc is None:
            excluded.append({**clean, "error": "工作点不可达"})
            continue
        d, il = dc
        di = (c['vin'] - il*(model.dcr+model.rds))*d/(c['_fsw']*model.L)
        if il-di/2 <= 0:
            excluded.append({**clean, 'error':'DCM：CCM模型不适用，另行验证', 'IL_valley_A':r3(il-di/2)})
            continue
        lin = model.linearize(c["vin"], d, il, vb, c["I0"], rac)
        scale = 1/rac if mode == "CC" else (c['Rac']/rac if rac else 1.0)
        g = [Boost.tf(lin, f) * scale for f in freqs]
        ctot = sum(C for C, _ in model.caps)
        meta = {**clean, "D": r3(d), "IL_A": r3(il), 'IL_valley_A':r3(il-di/2), "G0": r3(abs(g[0])),
                "f0_Hz": r3((1 - d) / (2 * math.pi * math.sqrt(model.L * ctot))),
                "fRHPZ_Hz": r3((1 - d) ** 2 * (c["Rac"] or 1e9) / (2 * math.pi * model.L)) if c["Rac"] else None}
        plants.append(g)
        info.append(meta)
    dyn = [ctrl_resp(f, Tc, ffilt) for f in freqs]
    if not plants:
        return {'mode':mode,'ok':False,'corners':info,'excluded_corners':excluded}
    g0 = max(abs(g[0]) for g in plants)
    best = None
    for kp_n in logspace(1e-3, 1.0, 31):
        for ki_n in logspace(5, 2e4, 41):
            kp, ki = kp_n / g0, ki_n / g0
            res = []
            ok = True
            for g in plants:
                Lw = [(kp + ki * Tc / (1 - z1)) * dz * gp for (z1, dz), gp in zip(dyn, g)]
                m = margins(freqs, Lw)
                if m is None or m[1] < pm_min or m[2] < gm_min:
                    ok = False
                    break
                res.append(m)
            if ok:
                fmin = min(m[0] for m in res)
                if best is None or fmin > best[0]:
                    best = (fmin, kp, ki, res)
    if best is None:
        return {"mode": mode, "ok": False, "corners": info, 'excluded_corners':excluded}
    fmin, kp, ki, res = best
    for meta, (fc, pm, gm) in zip([m for m in info if "error" not in m], res):
        meta.update({"fc_Hz": r3(fc), "PM_deg": r3(pm, 3), "GM_dB": r3(gm, 3) if math.isfinite(gm) else ">60"})
    unit = "A" if mode == "CC" else "V"
    kp,ki = r3(kp,8),r3(ki,8)
    # Check the actual exported coefficients, not only their pre-rounding values.
    exported_ok = all((m := margins(freqs, [(kp+ki*Tc/(1-z))*dz*gp for (z,dz),gp in zip(dyn,g)])) is not None
                      and m[1]>=pm_min and m[2]>=gm_min for g in plants)
    return {"mode": mode, "ok": exported_ok, "Kp": kp, "Ki": ki, "Ki_Tc": r3(ki * Tc,8),
            "units": f"Kp: 1/{unit}（占空比每{unit}误差）；Ki: 1/({unit}·s)；Ki_Tc 为每拍积分增益",
            "fc_min_Hz": r3(fmin), "corners": info, 'excluded_corners':excluded,
            'coverage':'仅列出的CCM工况及敏感性场景；DCM/空载/切换未证明', "_plants": plants}


def bode_rows(freqs, plant, kp, ki, Tc, ffilt):
    rows = []
    for f, gp in zip(freqs, plant):
        z1, dz = ctrl_resp(f, Tc, ffilt)
        L = (kp + ki * Tc / (1 - z1)) * dz * gp
        rows.append((f, 20 * math.log10(abs(gp)), math.degrees(cmath.phase(gp)), 20 * math.log10(abs(L)), L))
    ph = unwrap_deg([r[4] for r in rows])
    return [(f, gm, gp, lm, p) for (f, gm, gp, lm, _), p in zip(rows, ph)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir")
    ap.add_argument("--db", default=str(KNOWLEDGE_DIR / "parts_db.json"))
    a = ap.parse_args()
    p = workdir_paths(a.workdir)
    calc, ch = load_json(p["calc"]), load_json(p["choices"])
    model, minfo = build(load_json(p["selection"]), load_json(a.db), calc, ch)
    ctl = ch["control"]
    Tc = 1 / ctl["f_ctrl_hz"]
    ffilt = calc["sensing"]["vout"]["filter_fc_hz"]
    lc = ch['losses']
    csa,_ = selected(load_json(p['selection']),load_json(a.db),'csa')
    current_poles = [1/(2*math.pi*lc['current_filter_r_ohm']*lc['current_filter_c_f']),
                     param(csa,'bandwidth_khz',('typ','max'))*1000]
    freqs = logspace(0.1, 0.98 * ctl["f_ctrl_hz"] / 2, 420)
    cv_c, cc_c = corners(calc)
    model_set=list(variants(load_json(p['selection']),load_json(a.db),calc,ch))
    def expanded(cs):
        return [{**c,'variant':name,'factors':mi['factors'],'_model':m,
                 '_shunt':calc['sensing']['iout']['shunt_ohm'],'_fsw':calc['inputs']['fsw_hz']} for name,m,mi in model_set for c in cs]
    out = {"schema": "boost-loop/1", "revision": ch.get("revision"), "model": minfo,
           "assumptions": [f"控制频率 {ctl['f_ctrl_hz']} Hz，一拍计算延迟 + ZOH", f"采样 RC 滤波 fc≈{ffilt:.0f} Hz",
                           'CC用1k/10nF RC及INA180A2 210kHz典型极点；CV用实际分压RC',
                           "仅覆盖列出的CCM工况；DCM、空载、停脉冲及非线性CC/CV切换均未证明稳定；ESR范围是敏感性假设",
                           "电子负载 CC 模式下测 CV、CR 模式下测 CC；CC 负载测 CC 无确定工作点"]}
    plots = {}
    for mode, cs in (("CV", cv_c), ("CC", cc_c)):
        poles=ffilt if mode=='CV' else current_poles
        r = design_mode(model, expanded(cs), mode, Tc, poles, ctl["pm_min_deg"], ctl["gm_min_db"], freqs)
        plants = r.pop("_plants", None)
        out[mode] = r
        if r["ok"]:
            worst = min(range(len(plants)), key=lambda i: r["corners"][i]["PM_deg"])
            plots[mode] = (bode_rows(freqs, plants[worst], r["Kp"], r["Ki"], Tc, poles), r["corners"][worst])
            r["worst_corner"] = f"{r['corners'][worst]['variant']} Vin_{r['corners'][worst]['vin_key']} {r['corners'][worst]['load']}"
            csv = Path(a.workdir) / f"bode_{mode}.csv"
            csv.write_text("f_Hz,plant_dB,plant_deg,loop_dB,loop_deg\n" + "\n".join(
                f"{f:.4g},{a1:.3f},{b1:.2f},{c1:.3f},{d1:.2f}" for f, a1, b1, c1, d1 in plots[mode][0]), encoding="utf-8")
        print(f"{mode}: " + (f"Kp={r['Kp']}, Ki={r['Ki']} → 最差角 fc={r['fc_min_Hz']} Hz, "
                             f"PM≥{min(c['PM_deg'] for c in r['corners'] if 'PM_deg' in c)}°" if r["ok"] else "未找到满足裕度的 PI"))
    save_json(p["loop"], out)
    if plots:
        series = [{"label": f"{m} 环路 (Vin_{c['vin_key']}, {c['load']})", "f": [x[0] for x in rows],
                   "mag_db": [x[3] for x in rows], "phase_deg": [x[4] for x in rows]} for m, (rows, c) in plots.items()]
        svg = bode_svg(series, title="CC/CV 环路增益 Bode 图（最差工况角）")
        (Path(a.workdir) / "bode_loops.svg").write_text(svg, encoding="utf-8")
        print("bode_loops.svg written")

if __name__ == "__main__":
    main()
