"""阶段 7a — 用入选器件复核：工作点、损耗分解、效率、结温、输出纹波、增益峰值与上电浪涌.

计算模型在 loss_model.py：含寄生参数的平均模型求实际占空比与电流，用 parts_db 中有出处的器件参数
逐项计算损耗；在标称值之外，对电感/储能电容/陶瓷电容容差与储能电容 ESR 敏感性做全组合扫描
（stage_params.variants），给出容差包络（envelope）。若标称效率与 calc 假设 η 偏差 > 3 个百分点，
提示更新 design_choices.power_stage.eta_assumed 后重跑（run_all.py 会自动迭代）。

用法: python loss_thermal.py <workdir> [--db parts_db.json]
"""

import argparse

from _common import KNOWLEDGE_DIR, load_json, save_json, workdir_paths
from loss_model import run


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir")
    ap.add_argument("--db", default=str(KNOWLEDGE_DIR / "parts_db.json"))
    a = ap.parse_args()
    p = workdir_paths(a.workdir)
    res = run(load_json(p["calc"]), load_json(p["choices"]), load_json(p["selection"]), load_json(a.db))
    save_json(p["losses"], res)
    n, env = res["cases"]["nom"], res["envelope"]
    print(f"η(nom)={n['eta_total']*100:.2f}% (功率级 {n['eta_power_stage']*100:.2f}%), ΣP={n['p_total_W']} W；"
          f"容差最低 η={env['eta_min']*100:.2f}%；最大纹波 {res['ripple']['ripple_pct']}%；"
          f"Tj,max={env['tj_max_C']}；浪涌线性估计 {res['inrush']['i_peak_a']} A")
    ec = res["eta_consistency"]
    print("η 一致性：", "需要迭代 —— " + ec["advice"] if ec["needs_iteration"] else "假设与复核一致（偏差 ≤ 3 个百分点）")


if __name__ == "__main__":
    main()
