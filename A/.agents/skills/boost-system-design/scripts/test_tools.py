"""工具自检：用解析解验证模型与计算（python -m unittest test_tools -v）."""

import cmath
import math
import unittest

from _common import TEMPLATE_DIR, load_json, pick_at_least, pick_nearest
from boost_model import Boost
from calc_boost import calc
from loss_model import ripple

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
        vpp, _, _ = ripple(d, ipk, ival, f, [(C, 1e-6)], 1e9, nh=200)
        simple = (1 - d) * (ipk + ival) / 2 * d / (f * C)
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

    def test_current_lsb_uses_adc_reference(self):
        """回归：电流 LSB 按 ADC 基准 Vref/(Rs·G)/(2^N−1)，不按放大器摆幅（Codex 测试运行发现）."""
        c = calc(REQ, load_json(TEMPLATE_DIR / "design_choices.default.json"))
        i = c["sensing"]["iout"]
        self.assertAlmostEqual(i["lsb_ma"], 3.3 / (i["shunt_ohm"] * i["gain"]) / 4095 * 1e3, places=4)

    def test_dcm_corner_detected(self):
        """回归：电感取 −20% 容差、负载 0.08 A 时谷值电流 < 0，属 DCM，CCM 环路结论不得覆盖该点；
        标称电感、0.5 A 额定点则为 CCM."""
        for L, io, dcm in ((68e-6 * 0.8, 0.08, True), (68e-6, 0.5, False)):
            m = Boost(L, 0.14, 0.04, 0.47, 0.016, [(6e-6, 0.0025), (100e-6, 0.024)])
            d, il = m.solve_dc(5.0, 12.0, io)
            valley = il - 5.0 * d / (1e5 * L) / 2
            self.assertEqual(valley <= 0, dcm, (L, io, valley))

    def test_series(self):
        self.assertEqual(pick_at_least(40.6e3), 43e3)
        self.assertAlmostEqual(pick_nearest(1.95e-9), 2.2e-9)


class TestKicad(unittest.TestCase):
    SYM = ('(kicad_symbol_lib (version 20251024) (symbol "BASE" (property "Footprint" "Package_TO_SOT_SMD:SOT-23-6") '
           '(symbol "BASE_1_1" (pin input line (at 0 0 0) (length 2.54) (name "IN+" (effects)) (number "6" (effects))) '
           '(pin output line (at 0 0 0) (length 2.54) (name "OUTH" (effects)) (number "2" (effects))))))')

    def test_parse_and_read_symbol(self):
        from kicad_lib import _FileSource, block_text, parse, read_symbol
        self.assertEqual(parse('(a "x \\"y\\"" (b 1))'), ["a", 'x "y"', ["b", "1"]])
        src = _FileSource.__new__(_FileSource)
        src.t = self.SYM
        s = read_symbol(src, "lib:BASE")
        self.assertEqual(s["pins"]["6"]["name"], "IN+")
        self.assertEqual(s["default_footprint"], "Package_TO_SOT_SMD:SOT-23-6")
        self.assertTrue(block_text(self.SYM, "symbol", "BASE").endswith("))))"))

    def test_verify_part(self):
        from kicad_lib import verify_part
        idx = {"symbols": {"L:S": {"exists": True, "pins": {"1": {}, "2": {}, "3": {}}}},
               "footprints": {"F:SOT23": {"exists": True, "pads": ["1", "2", "3"]},
                              "F:SOT23-5": {"exists": True, "pads": ["1", "2", "3", "4", "5"]},
                              "F:2P": {"exists": True, "pads": ["1", "2"]}}}
        k = {"symbol": "L:S", "footprint": "F:SOT23", "pinmap": {"G": "1", "S": "2", "D": "3"}, "status": "official"}
        self.assertEqual(verify_part({"kicad": k}, idx)[0], "PASS")
        self.assertEqual(verify_part({"kicad": {**k, "pinmap": {"G": "4"}}}, idx)[0], "FAIL")
        self.assertEqual(verify_part({"kicad": {**k, "footprint": "F:2P"}}, idx)[0], "FAIL")      # 符号引脚 3 无焊盘
        self.assertEqual(verify_part({"kicad": {**k, "footprint": "F:SOT23-5"}}, idx)[0], "WARN")  # 多余焊盘
        self.assertEqual(verify_part({"kicad": {**k, "symbol": None}}, idx)[0], "FAIL")

    def test_snapshot_covers_db(self):
        """真实快照：parts_db 引用的 lib_id 都已记录。"""
        from kicad_lib import INDEX, collect
        if not INDEX.exists():
            self.skipTest("无 KiCad 快照")
        idx, db = load_json(INDEX), load_json(TEMPLATE_DIR.parent / "knowledge" / "parts_db.json")
        syms, fps = collect(db)
        self.assertFalse([s for s in syms if s not in idx["symbols"]])
        self.assertFalse([f for f in fps if f not in idx["footprints"]])

    def test_mcu_pin_plan(self):
        from kicad_lib import INDEX, verify_mcu_pins
        if not INDEX.exists():
            self.skipTest("无 KiCad 快照")
        db = load_json(TEMPLATE_DIR.parent / "knowledge" / "parts_db.json")
        mcu = next(p for p in db["parts"] if p["id"] == "stm32g431cbt6")
        ok, detail = verify_mcu_pins(mcu, load_json(INDEX))
        self.assertTrue(ok, detail)


if __name__ == "__main__":
    unittest.main()
