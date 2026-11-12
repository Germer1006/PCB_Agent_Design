"""Deterministic S5/S6 regression; this does not execute C0-C3 LLM experiments.

python A/eval/run_eval.py --output A/outputs/eval_preflight_01
The output directory must be new. Every case, including failures, is retained.
"""

import argparse
import copy
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / ".agents/skills/boost-system-design/scripts"
sys.path.insert(0, str(SCRIPTS))
from _common import KNOWLEDGE_DIR, TEMPLATE_DIR, load_json, save_json


def text_sha256(path):
    """Match the same source across Git's Windows/Linux newline conversion."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def analytic_checks(req, choices, data):
    """Separate elementary checks; not an independent circuit-model validation."""
    vo, io = req["vout"]["set_v"], req["iout"]["cc_set_a"]
    eta = choices["power_stage"]["eta_assumed"]
    vin = req["vin"]["nom_v"] * (1 - req["vin"]["tol_pct"] / 100)
    expected = {"d_ideal": 1 - vin / vo, "d_est": 1 - eta * vin / vo,
                "iin_avg_a": vo * io / (eta * vin)}
    errors = []
    for field, value in expected.items():
        if not math.isclose(data["operating_points"]["min"][field], value, rel_tol=.001):
            errors.append(f"min.{field}: {data['operating_points']['min'][field]} != {value}")
    if data["protection"]["ovp_hw"]["value"] <= data["protection"]["ovp_sw"]["value"]:
        errors.append("OVP ordering")
    return errors


def invoke(script, work, db=None, timeout=60):
    command = [sys.executable, "-B", str(SCRIPTS / script), str(work)]
    if db:
        command += ["--db", str(db)]
    try:
        proc = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
        result = {"script": script, "returncode": proc.returncode,
                  "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired as err:
        def decode(value):
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
        result = {"script": script, "returncode": None, "timeout": True,
                  "stdout": decode(err.stdout), "stderr": decode(err.stderr)}
    save_json(work / (Path(script).stem + ".log.json"), result)
    return result


def run_case(case, output, db):
    work = output / case["id"]
    work.mkdir()
    req = load_json(HERE / "specs" / (case["id"] + ".json"))
    choices = copy.deepcopy(load_json(TEMPLATE_DIR / "design_choices.default.json"))
    choices["revision"] = "eval-" + case["id"]
    save_json(work / "requirements.json", req)
    save_json(work / "design_choices.json", choices)
    record = {"id": case["id"], "difficulty": case["difficulty"],
              "expected": case["expected"], "stages": [], "workdir": case["id"],
              "specification": {"vin_nom_v": req["vin"]["nom_v"], "vout_v": req["vout"]["set_v"],
                                "iout_a": req["iout"]["cc_set_a"], "ripple_max_pct": req["ripple"]["max_pct"]}}
    initial = invoke("calc_boost.py", work)
    record["stages"].append({k: v for k, v in initial.items() if k not in ("stdout", "stderr")})
    if case["expected"] == "reject_topology":
        error = load_json(work / "calc_error.json") if (work / "calc_error.json").exists() else {}
        ok = initial["returncode"] == 2 and "Vin_max < Vout" in error.get("reason", "")
        record.update(outcome="correct_rejection" if ok else "unexpected_result", verified=ok,
                      reason=error.get("reason", initial["stderr"]))
    elif initial["returncode"] != 0:
        record.update(outcome="calculation_error", verified=False, reason=initial["stderr"])
    else:
        calc = load_json(work / "calc.json")
        errors = analytic_checks(req, choices, calc)
        selection_log = invoke("select_parts.py", work, db)
        record["stages"].append({k: v for k, v in selection_log.items() if k not in ("stdout", "stderr")})
        selection = load_json(work / "selection.json") if (work / "selection.json").exists() else {}
        missing = selection.get("missing_categories", [])
        actual_missing = []
        for role, item in selection.get("categories", {}).items():
            if item["selected"] is None:
                actual_missing.append(role)
            else:
                candidate = next((c for c in item["candidates"] if c["id"] == item["selected"]), None)
                if not candidate or not candidate["pass"] or not all(r["ok"] for r in candidate["rules"] if r["hard"]):
                    errors.append(f"{role}: selected part violates hard rules")
        if set(missing) != set(actual_missing):
            errors.append("missing_categories does not match selected parts")
        expected_code = 2 if actual_missing or selection.get("errors") else 0
        expected_status = "needs_rework" if expected_code else "selected"
        if selection.get("status") != expected_status:
            errors.append("selection status does not match missing parts/errors")
        record.update(outcome="selection_blocked" if missing or selection.get("errors") else "selected",
                      verified=not errors and bool(selection) and selection_log["returncode"] == expected_code,
                      analytic_errors=errors, missing_categories=missing,
                      selected={k: v["selected"] for k, v in selection.get("categories", {}).items()})
    save_json(work / "result.json", record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=KNOWLEDGE_DIR / "parts_db.json")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be a new directory; keep previous failures for review")
    args.output.mkdir(parents=True)
    manifest = load_json(HERE / "manifest.json")
    records = [run_case(case, args.output, args.db) for case in manifest["cases"]]
    summary = {"schema": "boost-tool-eval/1", "scope": "deterministic_S5_S6_only",
               "hash_normalization": "CRLF_to_LF",
               "db_sha256": text_sha256(args.db),
               "source_sha256": {name: text_sha256(SCRIPTS / name)
                                  for name in ("calc_boost.py", "select_parts.py", "_common.py")},
               "model_runs": 0, "cases": records,
               "verified": sum(item["verified"] for item in records), "total": len(records)}
    save_json(args.output / "summary.json", summary)
    lines = ["# A 工具链需求变体回归", "", "仅运行 S5/S6；不是 C0–C3 模型消融实验，不代表完整设计通过。",
             "", "| 用例 | Vin→Vo / Iout | 难度 | 结果 | 缺件类别 | 行为验证 |", "|---|---|---|---|---|---|"]
    for row in records:
        spec = row["specification"]
        lines.append(f"| {row['id']} | {spec['vin_nom_v']}→{spec['vout_v']} V / {spec['iout_a']} A | {row['difficulty']} | {row['outcome']} | "
                     f"{', '.join(row.get('missing_categories', [])) or '—'} | {row['verified']} |")
    lines += ["", f"行为验证 {summary['verified']}/{summary['total']}。缺件是当前库在固定默认策略下的边界，不能据此宣称规格物理不可实现。",
              "", "S7–S9、环路/仿真指标、完整标准答案和真实模型对照需另行运行。所有失败及 stdout/stderr 留在各用例目录。"]
    (args.output / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{summary['verified']}/{summary['total']} verified; {args.output / 'results.md'}")
    return 0 if summary["verified"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
