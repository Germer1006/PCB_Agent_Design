"""一键运行模块一工具链：calc → select → loss → (η 迭代) → loop → validate_loop → check → export.

用法:
  python run_all.py <workdir> [--requirements inputs/course_requirements.json] [--db parts_db.json] [--max-iter 3]

- workdir 中没有 requirements.json / design_choices.json 时，分别从 --requirements 与模板复制。
- 若复核效率与假设偏差 > 3%，自动把 eta_assumed 改为复核值并重跑（最多 --max-iter 次），
  每次修改记录到 design_choices.rationale.iteration_log —— 即“执行 → 观测 → 重规划”闭环。
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from _common import KNOWLEDGE_DIR, TEMPLATE_DIR, load_json, save_json, workdir_paths

HERE = Path(__file__).resolve().parent


def step(script, workdir, db=None):
    cmd = [sys.executable, str(HERE / script), str(workdir)] + (["--db", str(db)] if db else [])
    print(f"\n$ {script}")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    sys.stdout.write(r.stdout)
    if r.returncode:
        sys.stderr.write(r.stderr)
        sys.exit(f"{script} failed")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir")
    ap.add_argument("--requirements")
    ap.add_argument("--db", default=str(KNOWLEDGE_DIR / "parts_db.json"))
    ap.add_argument("--max-iter", type=int, default=3)
    a = ap.parse_args()
    w = Path(a.workdir)
    w.mkdir(parents=True, exist_ok=True)
    p = workdir_paths(w)
    if not p["requirements"].exists():
        if not a.requirements:
            sys.exit("workdir 中没有 requirements.json，请用 --requirements 指定")
        shutil.copy(a.requirements, p["requirements"])
    if not p["choices"].exists():
        shutil.copy(TEMPLATE_DIR / "design_choices.default.json", p["choices"])
        print("design_choices.json 不存在，已从模板复制（请在阶段 3/4 按论证修改）")

    for it in range(a.max_iter + 1):
        step("calc_boost.py", w)
        step("select_parts.py", w, a.db)
        step("loss_thermal.py", w, a.db)
        loss = load_json(p["losses"])
        ec = loss["eta_consistency"]
        if not ec["needs_iteration"] or it == a.max_iter:
            break
        ch = load_json(p["choices"])
        new = round(ec["eta_model_nom"], 2)
        ch.setdefault("rationale", {}).setdefault("iteration_log", []).append(
            f"iter {it+1}: eta_assumed {ch['power_stage']['eta_assumed']} → {new}（复核效率 {ec['eta_model_nom']}）")
        ch["power_stage"]["eta_assumed"] = new
        save_json(p["choices"], ch)
        print(f"\n>>> η 假设与复核偏差过大，更新 eta_assumed = {new} 后重算")
    step("loop_design.py", w, a.db)
    step("validate_loop.py", w)
    step("check_design.py", w, a.db)
    step("export_handoff.py", w, a.db)


if __name__ == "__main__":
    main()
