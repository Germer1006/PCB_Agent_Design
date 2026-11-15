"""Agent-tool failure paths: refuse invalid inputs and preserve user constraints."""

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _common import KNOWLEDGE_DIR, TEMPLATE_DIR, load_json, save_json
from calc_boost import calc
from select_parts import select

HERE = Path(__file__).resolve().parent
A_ROOT = HERE.parents[3]


class TestAgentBoundaries(unittest.TestCase):
    def setUp(self):
        self.req = load_json(A_ROOT / "inputs/course_requirements.json")
        self.choices = load_json(TEMPLATE_DIR / "design_choices.default.json")
        self.db = load_json(KNOWLEDGE_DIR / "parts_db.json")

    def test_non_boost_requirement_is_rejected_before_calculation(self):
        self.req["vout"]["set_v"] = 5
        self.req["pout"]["rated_w"] = 2.5
        with self.assertRaisesRegex(ValueError, "Vin_max < Vout"):
            calc(self.req, self.choices)

    def test_invalid_numbers_and_unsupported_topology_are_rejected(self):
        for value in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(value=value):
                req = copy.deepcopy(self.req)
                req["iout"]["cc_set_a"] = value
                with self.assertRaisesRegex(ValueError, "Iout must"):
                    calc(req, self.choices)
        self.choices["topology"] = "sepic"
        with self.assertRaisesRegex(ValueError, "Only async_boost"):
            calc(self.req, self.choices)

    def test_infeasible_standard_inductance_has_actionable_error(self):
        self.choices["power_stage"]["inductor_std_values_uh"] = [1, 2]
        with self.assertRaisesRegex(ValueError, "No standard inductance"):
            calc(self.req, self.choices)

    def test_missing_requirement_is_reported_as_an_input_error(self):
        del self.req["vout"]
        with self.assertRaisesRegex(ValueError, "Missing or malformed.*vout"):
            calc(self.req, self.choices)

    def test_invalid_or_incompatible_override_never_falls_back(self):
        for requested in ("not-a-real-part", "si2302cds"):
            with self.subTest(requested=requested):
                self.choices["selection"]["overrides"] = {"mosfet": requested}
                result = select(calc(self.req, self.choices), self.choices, copy.deepcopy(self.db))
                self.assertIsNone(result["categories"]["mosfet"]["selected"])
                self.assertIn("mosfet", result["missing_categories"])
                self.assertEqual(result["status"], "needs_rework")
                self.assertTrue(result["errors"])

    def test_typo_in_override_category_is_not_silently_ignored(self):
        self.choices["selection"]["overrides"] = {"mosfett": "ao3400a"}
        result = select(calc(self.req, self.choices), self.choices, self.db)
        self.assertEqual(result["status"], "needs_rework")
        self.assertIn("mosfett", result["errors"][0])

    def test_pipeline_stops_after_failed_selection_and_retains_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            self.choices["selection"]["overrides"] = {"mosfet": "not-a-real-part"}
            save_json(work / "requirements.json", self.req)
            save_json(work / "design_choices.json", self.choices)
            proc = subprocess.run([sys.executable, "-B", str(HERE / "run_all.py"), str(work)],
                                  capture_output=True, timeout=30)
            self.assertEqual(proc.returncode, 2, proc.stderr.decode("utf-8", errors="replace"))
            self.assertTrue(load_json(work / "selection.json")["errors"])
            self.assertFalse((work / "losses.json").exists())
            self.assertFalse((work / "design_spec.json").exists())

    def test_impossible_cli_writes_reason_instead_of_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            self.req["vout"]["set_v"] = 5
            self.req["pout"]["rated_w"] = 2.5
            save_json(work / "requirements.json", self.req)
            save_json(work / "design_choices.json", self.choices)
            proc = subprocess.run([sys.executable, "-B", str(HERE / "calc_boost.py"), str(work)],
                                  capture_output=True, encoding="utf-8", timeout=30)
            self.assertEqual(proc.returncode, 2)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertIn("Vin_max < Vout", load_json(work / "calc_error.json")["reason"])
            self.assertFalse((work / "calc.json").exists())

    def test_blocked_sdrc_still_runs_ledger_and_never_exports(self):
        import run_all
        for code in (2, 3):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                save_json(work / "requirements.json", self.req)
                save_json(work / "design_choices.json", self.choices)
                save_json(work / "losses.json", {"eta_consistency": {"needs_iteration": False}})
                calls = []
                def stage(script, *args, **kwargs):
                    calls.append(script)
                    if script == "validate_loop.py":
                        self.assertEqual(str(args[1]), str(work / "custom_db.json"))
                    return code if script == "check_design.py" else 0
                with patch.object(sys, "argv", ["run_all.py", str(work), "--db", str(work / "custom_db.json")]), patch.object(run_all, "step", side_effect=stage):
                    with self.assertRaises(SystemExit) as error:
                        run_all.main()
                self.assertEqual(error.exception.code, code)
                self.assertEqual(calls[-2:], ["check_design.py", "ledger.py"])
                self.assertNotIn("export_handoff.py", calls)


if __name__ == "__main__":
    unittest.main()
