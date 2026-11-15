"""从 selection.json + parts_db.json 提取入选器件的寄生参数，构造 Boost 平均模型."""

import itertools
from _common import CALC_CONSTANTS, constant_input, param, part_input, source_input, step_input
from boost_model import Boost

CERAMIC_ESR_EACH_OHM = CALC_CONSTANTS["ceramic_esr_ohm"]


def selected(selection, db, cat):
    pid = selection["categories"].get(cat, {}).get("selected")
    if pid is None:
        return None, 0
    part = next(p for p in db["parts"] if p["id"] == pid)
    return part, selection["categories"][cat].get("selected_qty") or 1


def diode_line(d, evidence=None):
    """Piece-wise linear diode: VF = VF0 + Rd·I, from two datasheet points (typ preferred)."""
    pts = []
    names = ("vf_v_at_0a5", "vf_v_at_1a", "vf_v_at_2a", "vf_v_at_3a", "vf_v_at_5a")
    for name, i in zip(names, CALC_CONSTANTS["diode_fit_current_a"]):
        v = param(d, name, ("typ", "max"))
        if v is not None:
            pts.append((i, v))
            if evidence is not None:
                evidence.append(part_input(d, name, ("typ", "max")))
    if evidence is not None:
        if pts:
            evidence.append(constant_input("diode_fit_current_a"))
            if len(pts) == 1:
                evidence.append(constant_input("single_point_diode_split"))
        else:
            evidence.extend(constant_input(k) for k in ("default_diode_vf0_v", "default_diode_rd_ohm"))
    if len(pts) >= 2:
        (i1, v1), (i2, v2) = pts[0], pts[-1]
        rd = max((v2 - v1) / (i2 - i1), 0.0)
        return v1 - rd * i1, rd, "两点拟合 " + ", ".join(f"{v}V@{i}A" for i, v in pts)
    if len(pts) == 1:
        i1, v1 = pts[0]
        return v1 * 0.85, v1 * 0.15 / i1, f"单点 {v1}V@{i1}A，按 85%/15% 拆分（假设）"
    return 0.45, 0.05, "无 VF 数据，取 0.45 V + 50 mΩ（假设）"


def build(selection, db, calc, choices, factors=(1.0, 1.0, 1.0, 1.0), *, evidence=None):
    ps = choices["power_stage"]
    ind, _ = selected(selection, db, "inductor")
    q, _ = selected(selection, db, "mosfet")
    dio, _ = selected(selection, db, "diode")
    cc, ncc = selected(selection, db, "cout_ceramic")
    cb, ncb = selected(selection, db, "cout_bulk")
    lc = choices.get("losses", {})
    hot = lc.get("rds_hot_factor", CALC_CONSTANTS["default_hot_factor"])
    lf, bf, cf, ef = factors
    pm, _ = selected(selection, db, "pmos")
    fuse, _ = selected(selection, db, "fuse")

    def read(part, key, fields=("typ", "max", "min"), default=None):
        value = param(part, key, fields) if part else None
        if evidence is not None:
            if value is not None:
                evidence.append(part_input(part, key, fields))
            if not value and default:
                evidence.append(constant_input(default))
        return (value or CALC_CONSTANTS[default]) if default else value

    if evidence is not None:
        evidence.append(source_input("choice", choices, "losses.rds_hot_factor")
                        if "rds_hot_factor" in lc else constant_input("default_hot_factor"))
        for role in ("inductor", "mosfet", "diode", "cout_ceramic", "cout_bulk", "pmos", "fuse"):
            pid = selection["categories"].get(role, {}).get("selected")
            evidence.append(step_input(role, pid, f"selection:{role}.selected"))
    path = (read(pm, "rds_on_mohm_vgs4v5", ("max", "typ"), "zero") * hot
            + read(fuse, "r_mohm", ("max", "typ"), "zero")) * 1e-3
    if ind:
        L = read(ind, "inductance_uh")
    else:
        L = calc["inductor"]["l_selected_uh"]
        if evidence is not None:
            evidence.append(source_input("calc", calc, "inductor.l_selected_uh"))
    L = L * 1e-6 * lf
    dcr = read(ind, "dcr_mohm", ("max", "typ"), "default_inductor_dcr_mohm") * 1e-3
    rds = read(q, "rds_on_mohm_vgs4v5", ("max", "typ"), "default_mosfet_rds_mohm") * 1e-3
    if q:
        rds *= hot
    vf0, rd, vf_note = diode_line(dio or {}, evidence)
    if not dio:
        vf_note = "未选二极管"
    caps, cap_note = [], []
    if cc:
        ret = cc.get("bias_retention_12v") or ps["cout_ceramic_bias_retention"]
        cap_uf = read(cc, "capacitance_uf")
        if evidence is not None:
            evidence.append(step_input("陶瓷电容数量", ncc, "selection:cout_ceramic.qty"))
            evidence.append(step_input("直流偏压保持率", ret, f"part:{cc['id']}.bias_retention_12v@未标注")
                            if cc.get("bias_retention_12v") else
                            source_input("choice", choices, "power_stage.cout_ceramic_bias_retention"))
            evidence.append(constant_input("ceramic_esr_ohm"))
        c_eff = ncc * cap_uf * ret * 1e-6 * cf
        caps.append((c_eff, CERAMIC_ESR_EACH_OHM / ncc))
        cap_note.append(f"陶瓷 {ncc}×{param(cc, 'capacitance_uf')}µF×保持率{ret}×容差因子{cf} = {c_eff*1e6:.1f}µF，ESR {CERAMIC_ESR_EACH_OHM*1e3/ncc:.1f}mΩ(假设)")
    if cb:
        esr = read(cb, "esr_mohm_100khz", ("max", "typ"), "default_capacitor_esr_mohm") * 1e-3
        caps.append((ncb * read(cb, "capacitance_uf") * 1e-6 * bf, esr / ncb * ef))
        if evidence is not None:
            evidence.append(step_input("储能电容数量", ncb, "selection:cout_bulk.qty"))
        cap_note.append(f"电解 {ncb}×{param(cb, 'capacitance_uf')}µF×容差因子{bf}，ESR {esr*1e3/ncb*ef:.1f}mΩ；ESR因子为敏感性假设")
    if not caps:
        caps = [(calc["output_cap"]["c_eff_required_uf"] * 1e-6, 0.01)]
        if evidence is not None:
            evidence.extend([source_input("calc", calc, "output_cap.c_eff_required_uf"),
                             constant_input("default_output_esr_ohm")])
        cap_note.append("未选电容，按需求值建模")
    model = Boost(L, dcr + path, rds, vf0, rd, caps)
    info = {"L_uH": L * 1e6, "DCR_mohm": (dcr + path) * 1e3,
            "inductor_dcr_mohm": dcr * 1e3, "input_path_mohm": path * 1e3,
            "factors": list(factors), "Rds_hot_mohm": rds * 1e3, "rds_hot_factor": hot,
            "VF0_V": vf0, "Rd_mohm": rd * 1e3, "diode_model": vf_note, "caps": cap_note}
    return model, info


def variants(selection, db, calc, choices, *, evidence=None):
    """Nominal plus the full L/C tolerance endpoint and bulk ESR scenario product."""
    lc = choices.get("losses", {})
    keys = ("inductor_tolerance", "bulk_cap_tolerance", "ceramic_cap_tolerance", "bulk_esr_sensitivity_factors")
    settings = [lc.get(k, CALC_CONSTANTS["default_" + k]) for k in keys]
    lt, bt, ct, esr_factors = settings
    if evidence is not None:
        evidence.extend(source_input("choice", choices, "losses." + k) if k in lc else constant_input("default_" + k)
                        for k in keys)
    points = [(1., 1., 1., 1.)] + list(itertools.product(
        (1-lt, 1+lt), (1-bt, 1+bt), (1-ct, 1+ct), esr_factors))
    for i, factors in enumerate(points):
        model, info = build(selection, db, calc, choices, factors, evidence=evidence if i == 0 else None)
        yield ("nominal" if i == 0 else f"tol_{i:02d}"), model, info
