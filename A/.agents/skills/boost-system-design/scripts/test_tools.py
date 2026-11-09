"""工具自检：用解析解验证模型与计算（python -m unittest test_tools -v）."""

import cmath
import math
import unittest

from _common import TEMPLATE_DIR, load_json, pick_at_least, pick_nearest
from boost_model import Boost
from calc_boost import calc
from loss_thermal import ripple_harmonic

REQ = {
    "vin": {"nom_v": 5.0, "tol_pct": 5}, "vout": {"set_v": 12.0}, "iout": {"cc_set_a": 0.5},
    "pout": {"rated_w": 6.0},
}


class TestModel(unittest.TestCase):
    def setUp(self):
        self.m = Boost(68e-6, 0.0, 0.0, 0.0, 0.0, [(100e-6, 1e-6)])  # 无损

    def test_lossless_duty(self):
        d, il = self.m.solve_dc(5.0, 12.0, 0.5)
        self.assertAlmostEqual(d, 1 - 5 / 12, places=6)
        self.assertAlmostEqual(il, 0.5 / (5 / 12), places=6)

    def test_lossless_dc_gain(self):
        d, il = self.m.solve_dc(5.0, 12.0, 0.5)
        g0 = abs(Boost.tf(self.m.linearize(5.0, d, il, 12.0, 0.0, 24.0), 0.01))
        self.assertAlmostEqual(g0, 12 / (1 - d), delta=0.01 * 12 / (1 - d))

    def test_rhpz_phase(self):
        """f ≫ f0 时：LC 贡献 −180°，RHPZ 在 fz 处再贡献 −45° → 总相位 ≈ −225°（即 +135°）."""
        d, il = self.m.solve_dc(5.0, 12.0, 0.5)
        fz = (1 - d) ** 2 * 24 / (2 * math.pi * 68e-6)
        ph = math.degrees(cmath.phase(Boost.tf(self.m.linearize(5.0, d, il, 12.0, 0.0, 24.0), fz)))
        self.assertAlmostEqual(ph % 360, 135, delta=6)

    def test_gain_peak_lossless_monotonic(self):
        dpk, vpk = self.m.gain_peak(5.0, 24)
        self.assertGreater(dpk, 0.98)


class TestRipple(unittest.TestCase):
    def test_ideal_cap(self):
        d, ipk, ival, f, C = 0.6, 1.4, 1.2, 1e5, 47e-6
        vpp, simple = ripple_harmonic(d, ipk, ival, f, [(C, 1e-6)], 1e9, n_harm=200)
        self.assertAlmostEqual(vpp, simple, delta=0.08 * simple)


class TestCalc(unittest.TestCase):
    def test_course_point(self):
        c = calc(REQ, load_json(TEMPLATE_DIR / "design_choices.default.json"))
        self.assertAlmostEqual(c["operating_points"]["min"]["d_ideal"], 1 - 4.75 / 12, places=3)
        self.assertEqual(c["pwm"]["arr"], 1699)
        self.assertEqual(c["inductor"]["l_selected_uh"], 68)
        s = c["sensing"]["vout"]
        self.assertAlmostEqual(s["code_at_vset"], round(12 * s["ratio"] / 3.3 * 4095), delta=1)
        p = c["protection"]
        self.assertTrue(12 < p["ovp_sw"]["value"] < p["ovp_hw"]["value"])
        self.assertLess(p["ocp_hw"]["adc_code"], 4095)

    def test_series(self):
        self.assertEqual(pick_at_least(40.6e3), 43e3)
        self.assertAlmostEqual(pick_nearest(1.95e-9), 2.2e-9)


if __name__ == "__main__":
    unittest.main()
