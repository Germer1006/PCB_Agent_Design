"""从 selection.json + parts_db.json 提取入选器件的寄生参数，构造 Boost 平均模型."""

from _common import param
from boost_model import Boost

CERAMIC_ESR_EACH_OHM = 0.005  # 陶瓷电容 ESR 手册通常不给，取 5 mΩ/只（假设，报告中注明）


def selected(selection, db, cat):
    pid = selection["categories"].get(cat, {}).get("selected")
    if pid is None:
        return None, 0
    part = next(p for p in db["parts"] if p["id"] == pid)
    return part, selection["categories"][cat].get("selected_qty") or 1


def diode_line(d):
    """Piece-wise linear diode: VF = VF0 + Rd·I, from two datasheet points (typ preferred)."""
    pts = []
    for name, i in (("vf_v_at_0a5", 0.5), ("vf_v_at_1a", 1.0), ("vf_v_at_2a", 2.0), ("vf_v_at_3a", 3.0), ("vf_v_at_5a", 5.0)):
        v = param(d, name, ("typ", "max"))
        if v is not None:
            pts.append((i, v))
    if len(pts) >= 2:
        (i1, v1), (i2, v2) = pts[0], pts[-1]
        rd = max((v2 - v1) / (i2 - i1), 0.0)
        return v1 - rd * i1, rd, "两点拟合 " + ", ".join(f"{v}V@{i}A" for i, v in pts)
    if len(pts) == 1:
        i1, v1 = pts[0]
        return v1 * 0.85, v1 * 0.15 / i1, f"单点 {v1}V@{i1}A，按 85%/15% 拆分（假设）"
    return 0.45, 0.05, "无 VF 数据，取 0.45 V + 50 mΩ（假设）"


def build(selection, db, calc, choices):
    ps = choices["power_stage"]
    ind, _ = selected(selection, db, "inductor")
    q, _ = selected(selection, db, "mosfet")
    dio, _ = selected(selection, db, "diode")
    cc, ncc = selected(selection, db, "cout_ceramic")
    cb, ncb = selected(selection, db, "cout_bulk")
    hot = choices.get("losses", {}).get("rds_hot_factor", 1.3)

    L = (param(ind, "inductance_uh") if ind else calc["inductor"]["l_selected_uh"]) * 1e-6
    dcr = (param(ind, "dcr_mohm", ("max", "typ")) or 100) * 1e-3 if ind else 0.1
    rds = (param(q, "rds_on_mohm_vgs4v5", ("max", "typ")) or 50) * 1e-3 * hot if q else 0.05
    vf0, rd, vf_note = diode_line(dio) if dio else (0.45, 0.05, "未选二极管")
    caps, cap_note = [], []
    if cc:
        ret = cc.get("bias_retention_12v") or ps["cout_ceramic_bias_retention"]
        c_eff = ncc * param(cc, "capacitance_uf") * ret * 1e-6
        caps.append((c_eff, CERAMIC_ESR_EACH_OHM / ncc))
        cap_note.append(f"陶瓷 {ncc}×{param(cc, 'capacitance_uf')}µF×保持率{ret} = {c_eff*1e6:.1f}µF，ESR {CERAMIC_ESR_EACH_OHM*1e3/ncc:.1f}mΩ(假设)")
    if cb:
        esr = (param(cb, "esr_mohm_100khz", ("max", "typ")) or 150) * 1e-3
        caps.append((ncb * param(cb, "capacitance_uf") * 1e-6, esr / ncb))
        cap_note.append(f"电解 {ncb}×{param(cb, 'capacitance_uf')}µF，ESR {esr*1e3/ncb:.0f}mΩ")
    if not caps:
        caps = [(calc["output_cap"]["c_eff_required_uf"] * 1e-6, 0.01)]
        cap_note.append("未选电容，按需求值建模")
    model = Boost(L, dcr, rds, vf0, rd, caps)
    info = {"L_uH": L * 1e6, "DCR_mohm": dcr * 1e3, "Rds_hot_mohm": rds * 1e3, "rds_hot_factor": hot,
            "VF0_V": vf0, "Rd_mohm": rd * 1e3, "diode_model": vf_note, "caps": cap_note}
    return model, info
