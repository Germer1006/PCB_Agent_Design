"""T3 acceptance tests using generated handoffs in temporary directories."""

import csv
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from _common import KNOWLEDGE_DIR, load_json, save_json, workdir_paths
from check_consistency import check
from export_handoff import handoff_inputs, prepare_handoff, write_bom, write_tables, write_draft
from handoff_header import write_header
from check_design import run as check_design
from test_sdrc import baseline
from render_report import render_document, TEMPLATE, REPORT

WORK = Path(__file__).resolve().parents[4] / "outputs" / "r6"


class TestConsistency(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        for name in ("requirements.json", "design_choices.json", "calc.json", "selection.json",
                     "losses.json", "loop.json", "check.json"):
            shutil.copy(WORK / name, self.work / name)
        # Synthetic qualified-supply fixture, NOT proof that real r6 passes T4.
        args = baseline(qualified_supply=True)
        self.db = args[6]
        save_json(self.work / "design_choices.json", args[1])
        save_json(self.work / "check.json", check_design(*args))
        save_json(self.work / "sim.json", args[7])
        J = handoff_inputs(self.work)
        spec = prepare_handoff(J, self.db)
        save_json(self.work / "design_spec.json", spec)
        save_json(self.work / "pcb_constraints.json", spec["pcb_constraints"])
        write_bom(self.work / "bom_module1.csv", spec)
        write_header(self.work / "boost_params.h", J["calc"], J["loop"], J["choices"])
        write_tables(self.work / "report_tables.md", spec, J["calc"], J["selection"],
                     J["losses"], J["loop"], J["check"])

    def test_generated_qualified_fixture_handoff_is_consistent(self):
        self.assertEqual(check(self.work, self.db), [])
        spec = load_json(self.work / "design_spec.json")
        self.assertIn("net_naming", spec)
        self.assertIn("PWM_SW", str(spec["nets"]))

    def test_tampered_lcsc_identifier_is_reported_at_bom_row(self):
        path = self.work / "bom_module1.csv"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
        index = rows[0].index("立创编号")
        rows[1][index] = "C_INVALID"
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            csv.writer(stream).writerows(rows)
        errors = check(self.work, self.db)
        self.assertTrue(any("bom_module1.csv" in error and "C_INVALID" in error for error in errors))

    def test_tampered_firmware_gain_reports_header_line(self):
        path = self.work / "boost_params.h"
        text = path.read_text(encoding="utf-8")
        changed, count = re.subn(r"(#define\s+CV_KP\s+)[^\r\n]+", r"\g<1>999.0f", text)
        self.assertEqual(count, 1)
        path.write_text(changed, encoding="utf-8")
        self.assertTrue(any("boost_params.h:" in error and "CV_KP" in error
                            for error in check(self.work, self.db)))

    def test_report_placeholders_and_drift(self):
        source = "版本 {{choice:revision}}；电感 {{calc:L2}} uH；最低效率 {{loss:envelope.eta_min}}"
        (self.work / TEMPLATE).write_text(source, encoding="utf-8")
        body = render_document(workdir_paths(self.work), self.db, source)
        (self.work / REPORT).write_text(body, encoding="utf-8")
        self.assertEqual(check(self.work, self.db), [])
        self.assertIn("B/prepare_pcb_input.py", body)
        self.assertNotIn("design_result.json", body)
        (self.work / REPORT).write_text(body + "{{calc:missing}}", encoding="utf-8")
        self.assertTrue(any("占位符" in error for error in check(self.work, self.db)))

    def test_revision_drift_is_reported(self):
        path = self.work / "calc.json"
        calc = load_json(path)
        calc["revision"] = "stale"
        save_json(path, calc)
        self.assertTrue(any("calc.json.revision" in error for error in check(self.work, self.db)))

    def test_pcb_intent_cannot_be_hand_edited_into_a_pass(self):
        path = self.work / "pcb_constraints.json"
        data = load_json(path)
        self.assertTrue(all(g["max_span_mm"] is None for g in data["groups"]))
        data["groups"][0]["max_span_mm"] = 999
        save_json(path, data)
        self.assertTrue(any("pcb_constraints.json" in error for error in check(self.work, self.db)))

    def test_exploratory_draft_does_not_bypass_formal_handoff(self):
        inputs = handoff_inputs(self.work)
        inputs["check"]["status"] = "needs_rework"
        with self.assertRaisesRegex(ValueError, "交接禁止"):
            prepare_handoff(inputs, self.db)
        destination = self.work / "draft"
        spec = write_draft(inputs, self.db, destination)
        self.assertEqual(spec["status"], "needs_rework")
        self.assertFalse(spec["formal_handoff"])
        self.assertIn("A_DRAFT_NOT_FORMAL", spec["pcb_constraints"]["upstream"]["open_issues"])
        self.assertEqual({p.name for p in destination.iterdir()},
                         {"draft_design_spec.json", "pcb_constraints.json", "draft_manifest.json"})
        with self.assertRaisesRegex(ValueError, "独立目录"):
            write_draft(inputs, self.db, destination)

    def test_exploratory_draft_still_rejects_mixed_revisions(self):
        inputs = handoff_inputs(self.work)
        inputs["check"]["revision"] = "stale"
        with self.assertRaisesRegex(ValueError, "混用版本"):
            write_draft(inputs, self.db, self.work / "draft")
        self.assertFalse((self.work / "draft").exists())

    def test_new_protection_choice_cannot_reuse_old_passing_check(self):
        inputs = handoff_inputs(self.work)
        inputs["choices"]["protection"]["input_protection"]["strategy"] = "undecided"
        with self.assertRaisesRegex(ValueError, "重跑 SDRC"):
            prepare_handoff(inputs, self.db)


if __name__ == "__main__":
    unittest.main()
