import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from constraints import validate_constraints, check_layout, attach_profile
from placement import boost_profile, place
from model_strategy import validate_strategy
from router import _Grid
from helpers import contract, model


class ConstraintsTests(unittest.TestCase):
    def test_reject_unknown_pads_numbers_and_typos(self):
        for change in ("pin", "nan", "bool", "version", "typo"):
            with self.subTest(change=change):
                c = contract()
                if change == "pin": c["pad_pairs"][0]["a"][1] = "99"
                if change == "nan": c["pad_pairs"][0]["max_distance_mm"] = float("nan")
                if change == "bool": c["pad_pairs"][0]["max_distance_mm"] = True
                if change == "version": c["schema_version"] = "future-999"
                if change == "typo": c["pad_pairs"][0]["max_distnace_mm"] = 3
                with self.assertRaises(ValueError): validate_constraints(c, model())

    def test_remote_decoupler_fails_even_without_any_DRC_collision(self):
        c = contract()
        positions = {"R1": {"x": 5, "y": 5, "angle": 0}, "R2": {"x": 20, "y": 5, "angle": 0}}
        result = check_layout(model(), positions, c)
        self.assertEqual(result["status"], "failed")
        self.assertAlmostEqual(result["checks"][0]["measured_mm"], 15)
        c["pad_pairs"][0]["max_distance_mm"] = None
        self.assertEqual(check_layout(model(), positions, c)["status"], "needs_review")

    def test_no_contract_is_not_a_pass(self):
        self.assertEqual(check_layout(model(), {}, None)["status"], "needs_review")

    def test_fixed_positions_regions_and_keepouts_are_enforced(self):
        d, c = model(), contract()
        c["pad_pairs"] = []
        c["fixed"] = {"R1": {"x": 4, "y": 4, "angle": 90}}
        c["regions"] = [{"id": "right", "refs": ["R2"], "bounds_mm": [12, 2, 19, 18], "evidence": ["synthetic fixture"]}]
        c["keepouts"] = [{"id": "forbidden", "refs": ["R2"], "bounds_mm": [12, 2, 15, 18], "evidence": ["synthetic fixture"]}]
        validate_constraints(c, d)
        positions = place(d, 22, 20, attach_profile(boost_profile(d), c))
        self.assertEqual(positions["R1"], c["fixed"]["R1"])
        self.assertGreaterEqual(positions["R2"]["x"], 16)
        self.assertEqual(check_layout(d, positions, c)["status"], "passed")

    def test_infeasible_fixed_layout_is_reported(self):
        c = contract(); c["fixed"] = {"R1": {"x": 0, "y": 0, "angle": 0}}
        with self.assertRaises(ValueError): place(model(), 20, 20, attach_profile(boost_profile(model()), c))

    def test_model_cannot_silently_drop_invalid_actions(self):
        for proposal in ({"ordered_nets": ["TYPO"]},
                         {"ordered_nets": ["VIN"], "layout_pairs": [["R1", "1", "R2", "2"]]},
                         {"ordered_nets": ["VIN"], "arbitrary_action": "delete_component"}):
            with self.assertRaises(ValueError): validate_strategy(proposal, model())

    def test_grid_extension_requires_an_explicit_budget(self):
        with self.assertRaises(ValueError): _Grid(90, 65, .05)
        self.assertGreater(_Grid(90, 65, .05, 2_500_000).area, 650_000)


if __name__ == "__main__": unittest.main()
