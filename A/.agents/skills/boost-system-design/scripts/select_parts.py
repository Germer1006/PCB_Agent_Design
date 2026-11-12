"""阶段 6 — 主要器件初步选型（基于器件知识库的规则筛选）.

读取 calc.json + design_choices.json + knowledge/parts_db.json，对每个类别的每个候选
逐条执行规则检查（硬性规则不满足即淘汰，缺少数据也淘汰），再对通过者按评分排序。
输出 selection.json：每个候选的检查明细、评分与入选器件，供报告“选型依据表”直接引用。

规则只使用 parts_db 中有出处的数据；数据库中没有的器件不能被选中。
可在 design_choices.selection.overrides 中按类别指定 part id（须仍通过硬性规则）。

用法: python select_parts.py <workdir> [--db path/to/parts_db.json]
"""

import argparse
import math

from _common import KNOWLEDGE_DIR, load_json, param, r3, save_json, workdir_paths
from stage_params import diode_line

MIN_CERAMIC_OUT = 2  # 输出端至少 2 只陶瓷电容紧贴功率回路作高频去耦
INPUT_PATH_DROP_V = 0.15  # 保险丝 + 防反接 PMOS 压降估计（check_design 用实际器件复核）
TIE_BAND = 0.10  # 评分相差 10% 以内视为等价，按价格选
HAND_SOLDER_BAD = ("QFN", "DFN", "BGA", "WLCSP", "SON", "LGA")


def hand_ok(p):
    pkg = p.get("package", "").upper()
    return not any(b in pkg for b in HAND_SOLDER_BAD)


def need(v, label):
    """Raise-free accessor: returns (value, missing_message)."""
    return (v, None) if v is not None else (None, f"{label} 数据缺失")


class Ctx:
    def __init__(self, calc, choices):
        self.c, self.ch = calc, choices
        i = calc["inputs"]
        self.vin_min, self.vin_max = i["vin"]["min"], i["vin"]["max"]
        self.vo, self.io, self.fsw = i["vout_v"], i["iout_cc_a"], i["fsw_hz"]
        self.ipk = calc["inductor"]["ipk_a"]
        self.irms_l = calc["inductor"]["irms_a"]
        self.iq_rms = calc["stress"]["mosfet_irms_a"]
        self.iin_max = calc["operating_points"]["min"]["iin_avg_a"]
        self.v_ovp_hw = calc["protection"]["ovp_hw"]["value"]
        self.k_v = choices["power_stage"]["voltage_derating"]
        self.vdrv = self.vin_min
        self.bulk_ceff_uf = 0.0


def check(rules, name, ok, detail, hard=True):
    rules.append({"rule": name, "ok": bool(ok), "hard": hard, "detail": detail})


# ---------------------------------------------------------------- per-category rules
def r_mosfet(p, x):
    R = []
    vds, rds, idc, vgsm = (param(p, "vds_max_v"), param(p, "rds_on_mohm_vgs4v5", ("max", "typ")),
                           param(p, "id_a"), param(p, "vgs_max_v"))
    req = x.c["stress"]["mosfet_vds_required_v"]
    check(R, "Vds 额定 ≥ 应力需求", vds is not None and vds >= req, f"{vds} V vs ≥{req} V")
    check(R, "Rds(on)@Vgs=4.5V 有规格", rds is not None, f"{rds} mΩ (max 优先)")
    check(R, "Id ≥ 2×IL,pk", idc is not None and idc >= 2 * x.ipk, f"{idc} A vs ≥{2*x.ipk:.2f} A")
    check(R, "Vgs 额定 ≥ 1.4×驱动电压", vgsm is not None and vgsm >= 1.4 * x.vin_max, f"{vgsm} V vs ≥{1.4*x.vin_max:.1f} V")
    check(R, "封装可手工焊接", hand_ok(p), p.get("package"))
    qg = param(p, "qg_nc") or 10
    score = None
    if rds is not None:
        p_cond = x.iq_rms ** 2 * rds * 1e-3 * 1.3
        p_sw = 0.5 * (x.v_ovp_hw) * x.ipk * (qg * 1e-9 / 1.0) * 2 * x.fsw  # ~1 A effective gate current
        score = p_cond + p_sw
    return R, score, "估算损耗 W（导通×1.3 热系数 + 开关）"


def r_pmos(p, x):
    R = []
    vds, rds, idc, vgsm = (abs(param(p, "vds_max_v") or 0) or None, param(p, "rds_on_mohm_vgs4v5", ("max", "typ")),
                           abs(param(p, "id_a") or 0) or None, abs(param(p, "vgs_max_v") or 0) or None)
    check(R, "|Vds| ≥ 2×Vin,max", vds is not None and vds >= 2 * x.vin_max, f"{vds} V")
    check(R, "|Id| ≥ 2×Iin,max", idc is not None and idc >= 2 * x.iin_max, f"{idc} A vs ≥{2*x.iin_max:.2f} A")
    check(R, "|Vgs| 额定 ≥ 1.4×Vin,max", vgsm is not None and vgsm >= 1.4 * x.vin_max, f"{vgsm} V")
    check(R, "Rds(on)@|Vgs|=4.5V 有规格", rds is not None, f"{rds} mΩ")
    check(R, "封装可手工焊接", hand_ok(p), p.get("package"))
    score = x.iin_max ** 2 * rds * 1e-3 * 1.3 if rds else None
    return R, score, "导通损耗 W"


def r_diode(p, x):
    R = []
    vr, ifav = param(p, "vrrm_v"), param(p, "if_avg_a")
    has_vf = any(param(p, n) is not None for n in ("vf_v_at_0a5", "vf_v_at_1a", "vf_v_at_2a", "vf_v_at_3a", "vf_v_at_5a"))
    vf1 = None
    if has_vf:
        vf0, rd, _ = diode_line(p)
        vf1 = round(vf0 + rd * 1.0, 3)
    req = x.c["stress"]["diode_vr_required_v"]
    check(R, "VRRM ≥ 反压需求", vr is not None and vr >= req, f"{vr} V vs ≥{req} V")
    check(R, "IF(AV) ≥ IL,pk（脉冲峰值按平均额定保守校核）", ifav is not None and ifav >= x.ipk, f"{ifav} A vs ≥{x.ipk:.2f} A")
    check(R, "VF 曲线数据可用", vf1 is not None, f"VF@1A≈{vf1} V（由手册 VF 点线性插值）")
    check(R, "肖特基（无反向恢复）", "schottky" in p.get("category", "") or p.get("schottky", False), p.get("category"))
    check(R, "封装可手工焊接", hand_ok(p), p.get("package"))
    return R, (vf1 * x.io if vf1 else None), "导通损耗 W ≈ VF@1A×Io"


def r_inductor(p, x):
    R = []
    L, isat, irms = param(p, "inductance_uh"), param(p, "isat_a", ("min", "typ")), param(p, "irms_a", ("min", "typ"))
    dcr = param(p, "dcr_mohm", ("max", "typ"))
    ind = x.c["inductor"]
    check(R, "L 在 [(1−rounding_allowance)×L_req, 1.6×L_sel] 内", L is not None and ind["l_required_uh"] * (1 - x.ch["power_stage"].get("inductor_rounding_allowance", 0.0)) <= L <= 1.6 * ind["l_selected_uh"],
          f"{L} µH；需求 {ind['l_required_uh']:.1f} µH，向下取整允许 {x.ch['power_stage'].get('inductor_rounding_allowance', 0.0):.0%}；上限 {1.6*ind['l_selected_uh']:.0f} µH")
    check(R, "Isat ≥ 需求", isat is not None and isat >= ind["isat_required_a"], f"{isat} A vs ≥{ind['isat_required_a']} A")
    check(R, "Irms(温升) ≥ 需求", irms is not None and irms >= ind["irms_required_a"], f"{irms} A vs ≥{ind['irms_required_a']} A")
    check(R, "DCR 有规格", dcr is not None, f"{dcr} mΩ")
    check(R, "屏蔽结构（EMI/采样干扰）", p.get("shielded", False), "shielded" if p.get("shielded") else "非屏蔽", hard=False)
    return R, (x.irms_l ** 2 * dcr * 1e-3 if dcr else None), "铜损 W = IL,rms²·DCR(max)"


def r_cap_bulk_out(p, x):
    R = []
    v, c, esr = param(p, "voltage_v"), param(p, "capacitance_uf"), param(p, "esr_mohm_100khz", ("max", "typ"))
    oc = x.c["output_cap"]
    irip = param(p, "ripple_current_ma_100khz", ("max", "typ"))
    check(R, "耐压 ≥ 需求", v is not None and v >= oc["v_rating_required_v"], f"{v} V vs ≥{oc['v_rating_required_v']} V")
    check(R, "容量 ≥ 100 µF（储能/环路）", c is not None and c >= 100, f"{c} µF")
    check(R, "ESR 有规格（用于纹波与环路阻尼计算）", esr is not None, f"{esr} mΩ")
    check(R, "纹波电流额定 ≥ IC,rms（单独承担时）", irip is not None and irip / 1000 >= oc["ripple_current_a"],
          f"{irip} mA vs {oc['ripple_current_a']*1000:.0f} mA（与陶瓷并联时实际分担更少）", hard=False)
    if c is None or esr is None:
        return R, None, ""
    counts = esr <= oc["esr_max_mohm"]
    ret = x.ch["power_stage"]["cout_ceramic_bias_retention"]
    n_cer = max(MIN_CERAMIC_OUT, math.ceil(max(oc["c_eff_required_uf"] - (c if counts else 0), 0) / (22 * ret)))
    p["_ceff_counted_uf"] = c if counts else 0.0
    return R, n_cer + 0.001 * esr, (f"ESR {esr} mΩ {'≤' if counts else '>'} {oc['esr_max_mohm']} mΩ → "
                                    f"{'计入' if counts else '不计入'}纹波有效容量；评分=所需 22µF 陶瓷只数（越少越好）")


def r_cap_ceramic_out(p, x):
    R = []
    v, c, diel = param(p, "voltage_v"), param(p, "capacitance_uf"), p.get("dielectric", "")
    oc = x.c["output_cap"]
    check(R, "耐压 ≥ 需求", v is not None and v >= oc["v_rating_required_v"], f"{v} V vs ≥{oc['v_rating_required_v']} V")
    check(R, "介质 X5R/X7R", diel in ("X5R", "X7R"), diel)
    check(R, "封装可手工焊接", hand_ok(p), p.get("package"))
    if c is None:
        return R, None, ""
    ret = p.get("bias_retention_12v") or x.ch["power_stage"]["cout_ceramic_bias_retention"]
    src = "手册曲线" if p.get("bias_retention_12v") else "设计假设（同尺寸同介质类比）"
    c_bulk = x.bulk_ceff_uf
    need = max(oc["c_eff_required_uf"] - c_bulk, 0.0)
    n = max(MIN_CERAMIC_OUT, math.ceil(need / (c * ret)))
    p["_qty"] = n
    return R, n * (p.get("price_cny_approx") or 0.3) + 0.01 * n, (
        f"需 {n} 只：(Ceff 需求 {oc['c_eff_required_uf']} − 低 ESR 储能电容计入 {c_bulk:.0f}) µF / "
        f"({c} µF × 12 V 保持率 {ret}，{src})，且至少 {MIN_CERAMIC_OUT} 只作高频去耦；评分=总价")


def r_cap_in_bulk(p, x):
    R = []
    v, c = param(p, "voltage_v"), param(p, "capacitance_uf")
    check(R, "耐压 ≥ 1.5×Vin,max", v is not None and v >= 1.5 * x.vin_max, f"{v} V")
    check(R, "容量 ≥ 47 µF（抵消电源线电感）", c is not None and c >= 47, f"{c} µF")
    return R, (p.get("price_cny_approx") or 1), "评分=价格"


def r_terminal(p, x):
    R = []
    i = param(p, "rated_current_a")
    check(R, "额定电流 ≥ 2×Iin,max", i is not None and i >= 2 * x.iin_max, f"{i} A")
    p["_qty"] = 2
    return R, (p.get("price_cny_approx") or 1), "评分=价格；输入/输出各 1 只"


def r_header(p, x):
    R = []
    check(R, "2.54 mm 排针", param(p, "pitch_mm") == 2.54, f"{param(p, 'pitch_mm')} mm")
    return R, (p.get("price_cny_approx") or 0.2), "评分=价格"


def r_cap_in(p, x):
    R = []
    v, c = param(p, "voltage_v"), param(p, "capacitance_uf")
    check(R, "耐压 ≥ 1.5×Vin,max", v is not None and v >= 1.5 * x.vin_max, f"{v} V")
    check(R, "介质 X5R/X7R", p.get("dielectric") in ("X5R", "X7R"), p.get("dielectric"))
    if c is None:
        return R, None, ""
    ret = p.get("bias_retention_5v", 0.6)
    n = max(1, math.ceil(x.c["input_cap"]["c_required_uf"] / (c * ret)))
    p["_qty"] = n
    return R, n * (p.get("price_cny_approx") or 0.3), f"需 {n} 只"


def r_gate_driver(p, x):
    R = []
    vmin, vmax = param(p, "vdd_min_v", ("min", "typ")), param(p, "vdd_max_v", ("max", "typ"))
    uvlo, vih = param(p, "uvlo_on_v", ("max", "typ")), param(p, "vih_v", ("max", "typ"))
    vdd_lo = x.vin_min - INPUT_PATH_DROP_V
    check(R, f"保证工作电压下限 ≤ 驱动器最低供电 {vdd_lo:.2f} V（Vin,min − 输入通路压降）",
          vmin is not None and vmin <= vdd_lo, f"VDD,min={vmin} V")
    check(R, "VDD 上限 ≥ Vin,max", vmax is not None and vmax >= x.vin_max, f"VDD,max={vmax} V")
    check(R, f"UVLO 开启最大值 ≤ {vdd_lo:.2f} − 0.1 V", uvlo is not None and uvlo <= vdd_lo - 0.1, f"UVLO,max={uvlo} V")
    check(R, "输入兼容 3.3 V 逻辑 (VIH ≤ 2.6 V)", vih is not None and vih <= 2.6, f"VIH={vih} V")
    check(R, "封装可手工焊接", hand_ok(p), p.get("package"))
    check(R, "立创商城可购（项目约束）", bool(p.get("lcsc")), str(p.get("lcsc")))
    if None in (vmin, uvlo):
        return R, None, ""
    margin = vdd_lo - max(vmin, uvlo)
    score = (p.get("price_cny_approx") or 15) + 10 * max(0.0, 0.5 - margin)
    score += 5 if p.get("lifecycle_risk") else 0
    score += 3 if p.get("exposed_pad") else 0
    return R, score, (f"价格 + 10×max(0, 0.5−电压余量 {margin:.2f} V) + 停产风险 5 + 裸露焊盘手焊难度 3")


def r_csa(p, x):
    R = []
    s = x.c["sensing"]["iout"]
    gain, vcm, vsmin, vsmax = param(p, "gain"), param(p, "vcm_max_v"), param(p, "vs_min_v"), param(p, "vs_max_v")
    vos = param(p, "vos_uv", ("max", "typ"))
    check(R, "增益 = 计算值", gain == s["gain"], f"{gain} vs {s['gain']}")
    check(R, "共模上限 ≥ 1.2×Vovp,hw", vcm is not None and vcm >= 1.2 * x.v_ovp_hw, f"{vcm} V")
    check(R, "可由 3.3 V 供电（输出不超 ADC 量程）", vsmin is not None and vsmin <= 3.3 <= (vsmax or 0), f"{vsmin}–{vsmax} V")
    check(R, "封装可手工焊接", hand_ok(p), p.get("package"))
    err = abs(vos) * 1e-6 / (s["shunt_ohm"] * x.io) * 100 if vos is not None else None
    cond = p.get("params", {}).get("vos_uv", {}).get("condition", "")
    check(R, "失调误差 ≤ 稳态精度要求的一半（2.5%）", err is not None and err <= 2.5, f"{r3(err)} %（{cond}，最大值）")
    check(R, "失调误差 ≤ 1%（否则需实际高边共模条件下标定）", err is not None and err <= 1.0, f"{r3(err)} %", hard=False)
    pkg_pen = 2.0 if "SC70" in (p.get("package") or "").upper() or "SC-70" in (p.get("package") or "").upper() else 0.0
    return R, (err + pkg_pen if err is not None else None), "失调误差 %@0.5A（最大值）+ SC70 封装手焊难度 2"


def r_shunt(p, x):
    R = []
    s = x.c["sensing"]["iout"]
    r, pw, tol = param(p, "resistance_mohm"), param(p, "power_w"), param(p, "tolerance_pct")
    p_ocp = s["shunt_ohm"] * x.c["protection"]["ocp_hw"]["value"] ** 2
    check(R, "阻值 = 计算值", r is not None and abs(r - s["shunt_ohm"] * 1e3) < 1e-6, f"{r} mΩ vs {s['shunt_ohm']*1e3:.0f} mΩ")
    check(R, "功率 ≥ 2×P(Iocp,hw)", pw is not None and pw >= 2 * p_ocp, f"{pw} W vs ≥{2*p_ocp:.3f} W")
    check(R, "精度 ≤ 1%", tol is not None and tol <= 1, f"{tol} %")
    return R, (p.get("price_cny_approx") or 0.5), "评分=价格"


def r_ldo(p, x):
    R = []
    vinmax, iout = param(p, "vin_max_v"), param(p, "iout_ma")
    vdo = param(p, "dropout_mv", ("max", "typ"))
    allow = (x.vin_min - 3.3) * 1000 - 200
    i3 = x.c["aux"]["i_3v3_ma"]
    check(R, "Vin 额定 ≥ 1.2×Vin,max", vinmax is not None and vinmax >= 1.2 * x.vin_max, f"{vinmax} V")
    check(R, "输出电流 ≥ 2×I3V3", iout is not None and iout >= 2 * i3, f"{iout} mA vs ≥{2*i3:.0f} mA")
    check(R, "压差 ≤ (Vin,min−3.3) − 200 mV", vdo is not None and vdo <= allow,
          f"{vdo} mV ({p['params'].get('dropout_mv',{}).get('condition','')}) vs ≤{allow:.0f} mV")
    check(R, "封装可手工焊接", hand_ok(p), p.get("package"))
    acc = param(p, "accuracy_pct", ("max", "typ"))
    check(R, "输出精度有规格（3.3 V 兼作 ADC 基准）", acc is not None, f"±{acc}%")
    return R, ((acc or 5) + 0.1 * (p.get("price_cny_approx") or 1)), "输出精度 %（ADC 基准）+ 0.1×价格"


def r_tvs(p, x):
    R = []
    vbr, vrwm, vc = param(p, "vbr_min_v", ("min", "typ")), param(p, "vrwm_v"), param(p, "vc_v", ("max", "typ"))
    check(R, "VRWM ≥ Vovp,hw（正常及软/硬过压动作前不导通）", vrwm is not None and vrwm >= x.v_ovp_hw, f"{vrwm} V vs ≥{x.v_ovp_hw:.1f} V")
    check(R, "VBR,min > Vovp,hw", vbr is not None and vbr > x.v_ovp_hw, f"{vbr} V")
    return R, (vc or 99), "评分=钳位电压（越低越好，保护 25 V 电容/30 V MOSFET）"


def r_fuse(p, x):
    R = []
    ih = param(p, "i_hold_a") or param(p, "i_rated_a")
    vm = param(p, "v_max_v")
    check(R, "额定/保持电流 ≥ 1.25×Iin,max", ih is not None and ih >= 1.25 * x.iin_max, f"{ih} A vs ≥{1.25*x.iin_max:.2f} A")
    check(R, "额定/保持电流 ≤ 3×Iin,max", ih is not None and ih <= 3 * x.iin_max, f"{ih} A", hard=False)
    check(R, "额定电压 ≥ 6 V", vm is not None and vm >= 6, f"{vm} V")
    r = param(p, "r_mohm", ("max", "typ"))
    return R, (x.iin_max ** 2 * r * 1e-3 if r else 0.1), "压降损耗 W"


def r_mcu(p, x):
    R = []
    ftim, adcs, comps = param(p, "tim_clk_mhz"), param(p, "adc_count"), param(p, "comp_count")
    counts = ftim * 1e6 / x.fsw if ftim else None
    check(R, "定时器时钟有规格", counts is not None, f"{ftim} MHz")
    check(R, "PWM 计数 ≥ 1024（≥10 bit @fsw）", counts is not None and counts >= 1024,
          f"{r3(counts)} counts（不足时输出电压步长变粗，仍可用）", hard=False)
    check(R, "MCU 为 STM32（课程推荐）", "STM32" in (p.get("mpn") or ""), p.get("mpn"))
    check(R, "ADC 外设 ≥ 1", adcs is not None and adcs >= 1, f"{adcs}")
    check(R, "ADC 可用通道 ≥ 3", (param(p, "adc_channels") or 0) >= 3, f"{param(p, 'adc_channels')}")
    check(R, "封装可手工焊接", hand_ok(p), p.get("package"))
    check(R, "片内比较器可触发 TIM1 刹车（硬件 OVP/OCP）", bool(comps) and p.get("comp_to_tim1_break", False),
          f"COMP×{comps}", hard=False)
    check(R, "FPU（浮点 PI 实现简单）", p.get("fpu", False), str(p.get("fpu")), hard=False)
    score = (p.get("price_cny_approx") or 20)
    score += 0 if (comps and p.get("comp_to_tim1_break")) else 15
    score += 0 if p.get("fpu") else 5
    score += 0 if (counts or 0) >= 1500 else 3
    pitch = param(p, "pitch_mm")
    score += 0 if (pitch or 0) >= 0.8 else 2
    score += 0 if p.get("needs_hse") is False else 2
    score += 5 if p.get("stock_risk") else 0
    return R, score, ("价格(¥) + 惩罚：无可触发刹车的比较器 +15，无 FPU +5，PWM<1500 计数 +3，"
                      "0.5 mm 引脚 +2，需外部晶振 +2，缺货风险 +5")


RULESETS = {
    "mcu": r_mcu, "mosfet": r_mosfet, "pmos": r_pmos, "diode": r_diode, "inductor": r_inductor,
    "cout_bulk": r_cap_bulk_out, "cout_ceramic": r_cap_ceramic_out, "cin_ceramic": r_cap_in, "cin_bulk": r_cap_in_bulk,
    "gate_driver": r_gate_driver, "csa": r_csa, "shunt": r_shunt, "ldo": r_ldo, "tvs": r_tvs, "fuse": r_fuse,
    "terminal": r_terminal, "header_swd": r_header, "header_uart": r_header,
}


def select(calc, choices, db):
    x = Ctx(calc, choices)
    overrides = choices.get("selection", {}).get("overrides", {})
    out = {"schema": "boost-selection/1", "revision": choices.get("revision"), "categories": {}}
    for cat, fn in RULESETS.items():
        cands = [p for p in db["parts"] if p.get("role") == cat]
        evals = []
        for p in cands:
            rules, score, score_note = fn(p, x)
            check(rules, "数据手册与立创编号证据齐全且 confidence 为 high/medium", p.get("confidence") in ("high", "medium") and bool(p.get("datasheet_url")) and bool(p.get("lcsc")) and bool(p.get("lcsc_evidence")),
                  f"confidence={p.get('confidence')}，datasheet={bool(p.get('datasheet_url'))}，LCSC={p.get('lcsc')}")
            hard_ok = all(r["ok"] for r in rules if r["hard"])
            evals.append({"id": p["id"], "mpn": p.get("mpn"), "manufacturer": p.get("manufacturer"),
                          "package": p.get("package"), "lcsc": p.get("lcsc"), "price_cny_approx": p.get("price_cny_approx"),
                          "pass": hard_ok, "score": r3(score) if score is not None else None, "score_note": score_note,
                          "qty": p.get("_qty", 1), "stock_risk": bool(p.get("stock_risk")), "rules": rules,
                          "price_status": "知识库价格快照，采购前复核" if p.get("price_cny_approx") is not None else "价格缺失；评分含缺省估值，不是报价",
                          "datasheet_url": p.get("datasheet_url"), "confidence": p.get("confidence")})
        passing = [e for e in evals if e["pass"] and e["score"] is not None]
        chosen = None
        if cat in overrides:
            chosen = next((e for e in passing if e["id"] == overrides[cat]), None)
            if chosen is None:
                out.setdefault("errors", []).append(f"{cat}: override {overrides[cat]} 未通过硬性规则或不存在")
        tie_note = ""
        if chosen is None and passing:
            best = min(passing, key=lambda e: e["score"])
            band = [e for e in passing if e["score"] <= best["score"] * (1 + TIE_BAND) + 1e-12]
            chosen = min(band, key=lambda e: (e["stock_risk"], e["price_cny_approx"] if e["price_cny_approx"] is not None else 99, e["score"]))
            if chosen is not best:
                tie_note = (f"；评分与最优 {best['mpn']}（{best['score']}）相差 ≤{TIE_BAND:.0%}，视为无显著差异，"
                            f"先无缺货风险，再价格低/价格可查优先")
        if cat == "cout_bulk" and chosen:
            x.bulk_ceff_uf = next(p for p in cands if p["id"] == chosen["id"]).get("_ceff_counted_uf", 0.0)
        out["categories"][cat] = {"candidates": evals, "selected": chosen["id"] if chosen else None,
                                  "selected_qty": chosen["qty"] if chosen else None,
                                  "reason": (f"通过全部硬性规则；评分（{chosen['score']}，{chosen['score_note']}）"
                                             + tie_note + ("；由 overrides 指定" if cat in overrides and chosen else ""))
                                  if chosen else "无候选通过 — 需补充器件库或修改设计决策"}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir")
    ap.add_argument("--db", default=str(KNOWLEDGE_DIR / "parts_db.json"))
    a = ap.parse_args()
    p = workdir_paths(a.workdir)
    res = select(load_json(p["calc"]), load_json(p["choices"]), load_json(a.db))
    save_json(p["selection"], res)
    for cat, v in res["categories"].items():
        n = len(v["candidates"])
        ok = sum(c["pass"] for c in v["candidates"])
        print(f"{cat:13s} {ok}/{n} 通过 → {v['selected']}" + (f" ×{v['selected_qty']}" if (v['selected_qty'] or 1) > 1 else ""))
    for e in res.get("errors", []):
        print("ERROR:", e)


if __name__ == "__main__":
    main()
