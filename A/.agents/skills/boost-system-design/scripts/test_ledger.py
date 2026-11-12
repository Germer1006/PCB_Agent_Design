"""核对电感计算的来源和过期值检测。"""

import unittest

from _common import TEMPLATE_DIR, load_json, step_input
from calc_boost import calc
from ledger import build_ledger
from trace_graph import build_graph, stale_records


class TestInductorLedger(unittest.TestCase):
    def setUp(self):
        self.req = {
            "vin": {"nom_v": 5.0, "tol_pct": 5},
            "vout": {"set_v": 12.0}, "iout": {"cc_set_a": 0.5},
            "pout": {"rated_w": 6.0},
        }
        self.choices = load_json(TEMPLATE_DIR / "design_choices.default.json")
        self.calc = calc(self.req, self.choices)

    def test_inductor_requires_max_of_three_traceable_corners(self):
        ledger = build_ledger(self.req, self.choices, self.calc)
        steps = {step["id"]: step for step in ledger["steps"]}
        l_req, l_selected = steps["L1"], steps["L2"]
        self.assertEqual({i["source"] for i in l_req["inputs"]},
                         {"step:L1_MIN", "step:L1_NOM", "step:L1_MAX"})
        self.assertEqual(l_req["value_exact"], max(i["value"] for i in l_req["inputs"]))
        self.assertAlmostEqual(l_req["value_exact"], 70.08, delta=0.01)
        self.assertEqual(l_selected["value_exact"], 68)
        self.assertIn("choice:power_stage.eta_assumed",
                      {i["source"] for i in steps["D2_MAX"]["inputs"]})
        self.assertIn("req:vin.tol_pct", {i["source"] for i in steps["P3_MAX"]["inputs"]})
        self.assertEqual(ledger["coverage"]["traced"], ledger["coverage"]["total"])

    def test_ledger_rejects_old_ripple_target_after_choice_edit(self):
        self.choices["power_stage"]["inductor_ripple_ratio"] = 0.36
        with self.assertRaisesRegex(ValueError, r"L1_MIN .*inductor_ripple_ratio"):
            build_ledger(self.req, self.choices, self.calc)

    def test_ledger_rejects_broken_reference(self):
        step = next(x for x in self.calc["steps"] if x["id"] == "L1_MAX")
        step["inputs"][0]["source"] = "step:nonexistent"
        with self.assertRaisesRegex(ValueError, r"L1_MAX .*nonexistent"):
            build_ledger(self.req, self.choices, self.calc)

    def test_ledger_rejects_step_without_inputs(self):
        del self.calc["steps"][-1]["inputs"]
        with self.assertRaisesRegex(ValueError, "A3 缺少来源"):
            build_ledger(self.req, self.choices, self.calc)

    def test_drift_propagates_through_selection_to_rules_and_handoff(self):
        inductor, mosfet = "L_MWSA1206S_680MT", "ao3400a"
        steps = [{**step, "stage": "calc"} for step in self.calc["steps"]]
        parts = [
            step_input("L", 68, f"part:{inductor}.inductance_uh.typ@13 of 38"),
            step_input("Rds", 26, f"part:{mosfet}.rds_on_mohm_vgs4v5.max@1"),
        ]
        steps.extend({"id": sid, "stage": "loss", "inputs": parts}
                     for sid in ("E1", "L_PK_MAX", "L_RMS_MAX", "L_RIPPLE_MAX", "R2"))
        steps.append({"id": "CV_KP", "stage": "loop", "inputs": parts})
        selection = {"categories": {"inductor": {"selected": inductor}, "mosfet": {"selected": mosfet}}}
        check = {"checks": [{"id": rid, "ref": "PWR", "verdict": "PASS"}
                            for rid in ("SR34", "SR36", "SR37", "SR38")]}
        graph = build_graph(steps, selection, check)

        cases = ((inductor, {"E1", "R2", "CV_KP"}, {"SR34", "SR36", "SR37", "SR38"}, "inductor"),
                 (mosfet, {"E1", "R2", "CV_KP"}, {"SR36", "SR38"}, "mosfet"),
                 ("inductor_ripple_ratio", {"L1", "L2", "E1", "CV_KP"}, {"SR38"}, "inductor"))
        for key, required_steps, required_rules, role in cases:
            with self.subTest(key=key):
                result = stale_records(graph, key)
                self.assertGreaterEqual(set(result["steps"]), required_steps)
                self.assertGreaterEqual(set(result["rules"]), required_rules)
                self.assertIn(f"bom_module1.csv.role[{role}]", result["handoff"])
                self.assertIn("boost_params.h.CV_KP", result["handoff"])


if __name__ == "__main__":
    unittest.main()
