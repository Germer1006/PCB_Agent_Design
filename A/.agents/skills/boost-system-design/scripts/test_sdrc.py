"""T4 regression tests for SDRC metadata, missing evidence and process gates."""

import unittest
import subprocess
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path

from _common import KNOWLEDGE_DIR, load_json, save_json
from check_design import RULES_PATH, _decorate_checks, _exit_code, run

HERE = Path(__file__).resolve().parent
WORK = HERE.parents[3] / "outputs" / "r6"


def baseline():
    req, choices, calc, selection, losses, loop = (
        load_json(WORK / name) for name in (
            "requirements.json", "design_choices.json", "calc.json",
            "selection.json", "losses.json", "loop.json"))
    db = load_json(KNOWLEDGE_DIR / "parts_db.json")
    # Synthetic success evidence exercises aggregation only; it is not a physical
    # simulation result. Real T1 evidence is checked separately with the CLI.
    checks = {key: True for key in ("SR80", "SR81", "SR82", "SR82-target", "SR83", "SR84", "SR85", "SR87")}
    sim = {"schema": "boost-switching-sim/1", "revision": choices["revision"],
           "cases": [{"name": "unit-fixture", "waveform": "unit-fixture.csv",
                      "verdict": "PASS", "checks": checks}],
           "cross_validation": {key: {"passed": True, "error_pct": 0, "limit_pct": 2}
                                for key in ("duty", "il_avg", "ripple_mv", "worst_ripple_mv")}}
    return req, choices, calc, selection, losses, loop, db, sim


class TestSdrc(unittest.TestCase):
    def test_baseline_metadata_and_conditional_review(self):
        result = run(*baseline())
        self.assertEqual(result["status"], "conditional_review")
        self.assertEqual(_exit_code(result), 0)
        self.assertTrue(all(all(key in item for key in (
            "layer", "severity", "rationale", "source", "verify", "applies_when", "closure_if_fail"))
            for item in result["checks"]))

    def test_missing_inductor_saturation_data_is_not_assessed(self):
        args = list(baseline())
        db = args[6]
        inductor_id = args[3]["categories"]["inductor"]["selected"]
        part = next(p for p in db["parts"] if p["id"] == inductor_id)
        del part["params"]["isat_a"]
        result = run(*args)
        check = next(c for c in result["checks"] if c["id"] == "SR36")
        self.assertEqual(check["verdict"], "NOT_ASSESSED")
        self.assertEqual(_exit_code(result), 3)

    def test_hardware_ovp_above_tvs_is_fail(self):
        args = list(baseline())
        calc = args[2]
        tvs_id = args[3]["categories"]["tvs"]["selected"]
        db_part = next(p for p in args[6]["parts"] if p["id"] == tvs_id)
        vbr = db_part["params"]["vbr_min_v"]["min"]
        calc["protection"]["ovp_hw"]["value"] = vbr + 1
        result = run(*args)
        check = next(c for c in result["checks"] if c["id"] == "SR30")
        self.assertEqual(check["verdict"], "FAIL")
        self.assertEqual(_exit_code(result), 2)

    def test_unregistered_rule_is_rejected(self):
        rules = load_json(RULES_PATH)
        with self.assertRaisesRegex(ValueError, "SR99"):
            _decorate_checks([{"id": "SR99"}], rules)

    def test_missing_simulation_blocks_handoff(self):
        args = list(baseline())
        args[-1] = None
        result = run(*args)
        self.assertEqual(_exit_code(result), 3)
        checks = {c["id"]: c for c in result["checks"]}
        self.assertEqual(checks["SR80"]["verdict"], "NOT_ASSESSED")
        self.assertEqual(checks["SR86"]["verdict"], "NOT_ASSESSED")
        self.assertEqual(checks["SR88"]["verdict"], "N/A")
        self.assertTrue(checks["SR88"]["detail"])

    def test_missing_capacitor_and_amplifier_data_are_not_zero(self):
        args = list(baseline())
        for category, fields in (("cout_bulk", ("voltage_v", "ripple_current_ma_100khz")),
                                 ("cout_ceramic", ("voltage_v",)), ("csa", ("vos_uv",))):
            pid = args[3]["categories"][category]["selected"]
            part = next(p for p in args[6]["parts"] if p["id"] == pid)
            for field in fields:
                part["params"].pop(field, None)
        checks = {c["id"]: c for c in run(*args)["checks"]}
        for rule in ("SR32", "SR39", "SR44"):
            self.assertEqual(checks[rule]["verdict"], "NOT_ASSESSED")

    def test_selected_part_absent_from_database_is_not_assessed(self):
        args = list(baseline())
        args[3]["categories"]["ldo"]["selected"] = "missing-part-id"
        result = run(*args)
        checks = {c["id"]: c for c in result["checks"]}
        self.assertEqual(checks["SR70"]["verdict"], "NOT_ASSESSED")
        self.assertEqual(checks["SR11-ldo"]["verdict"], "NOT_ASSESSED")
        self.assertEqual(_exit_code(result), 3)

    def test_empty_cross_validation_and_missing_scenario_are_not_pass(self):
        args = list(baseline())
        args[-1]["cross_validation"] = {}
        args[-1]["cases"][0]["checks"].pop("SR85")
        checks = {c["id"]: c for c in run(*args)["checks"]}
        self.assertEqual(checks["SR86"]["verdict"], "NOT_ASSESSED")
        self.assertEqual(checks["SR85"]["verdict"], "NOT_ASSESSED")

    def test_malformed_metadata_is_rejected(self):
        rules = load_json(RULES_PATH)
        rules[0]["severity"] = "invalid"
        with self.assertRaisesRegex(ValueError, "severity"):
            _decorate_checks([{"id": "SR01"}], rules)

    def test_cli_exit_codes_and_direct_export_gate(self):
        args = list(baseline())
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            names = ("requirements.json", "design_choices.json", "calc.json", "selection.json",
                     "losses.json", "loop.json", "parts_db.json", "sim.json")
            for name, data in zip(names, args):
                save_json(work / name, data)
            command = [sys.executable, "-B", str(HERE / "check_design.py"), str(work),
                       "--db", str(work / "parts_db.json")]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            (work / "sim.json").unlink()
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 3)
            exported = subprocess.run([sys.executable, "-B", str(HERE / "export_handoff.py"),
                                       str(work), "--db", str(work / "parts_db.json")], capture_output=True)
            self.assertNotEqual(exported.returncode, 0)
            self.assertFalse((work / "design_spec.json").exists())
            args[2]["protection"]["ovp_hw"]["value"] = 100
            save_json(work / "calc.json", args[2])
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)

    def test_run_all_preserves_gate_exit_codes(self):
        from run_all import step
        for code in (2, 3):
            result = subprocess.CompletedProcess([], code, stdout="", stderr="")
            with patch("run_all.subprocess.run", return_value=result):
                with self.assertRaises(SystemExit) as raised:
                    step("check_design.py", "unused")
                self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()
