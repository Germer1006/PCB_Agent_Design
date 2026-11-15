"""功率级及闭环控制的验证，不依赖课程基线器件。"""

import unittest
import tempfile
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from _common import KNOWLEDGE_DIR, load_json
from boost_model import Boost
from sim_control import AnalogSense, ControlSettings, DigitalControl, Signals
from sim_scenarios import Cycle, Scenario, _case_metrics, _collect, _ocp_stimulus, run_suite
from sim_switching import (OperatingPoint, Sample, simulate_closed_loop,
                           simulate_fixed_duty, simulate_input_ramp)


class TestSwitchingStage(unittest.TestCase):
    def test_lossless_ccm_matches_ideal_conversion_ratio(self):
        vin, duty, load, fsw = 3.3, 0.5, 12.0, 100_000
        model = Boost(82e-6, 0.0, 0.0, 0.0, 0.0, [(68e-6, 1e-6)])
        target = vin / (1 - duty)
        i_avg = target / load / (1 - duty)
        i_ripple = vin * duty / (fsw * model.L)
        samples = list(simulate_fixed_duty(
            model, vin_v=vin, duty=duty, load_ohm=load, fsw_hz=fsw, cycles=80,
            initial_vout_v=target, initial_i_l_a=i_avg - i_ripple / 2,
        ))
        last = [s for s in samples if s.t_s >= 79 / fsw]
        v_avg = sum((a.v_out_v + b.v_out_v) / 2 * (b.t_s - a.t_s)
                    for a, b in zip(last, last[1:])) / (last[-1].t_s - last[0].t_s)
        self.assertAlmostEqual(v_avg, target, delta=0.01 * target)
        self.assertEqual({s.mode for s in last}, {"switch_on", "diode_on"})
        self.assertTrue(all(b.t_s - a.t_s <= 50e-9 + 1e-15
                            for a, b in zip(last, last[1:])))

    def test_light_load_reaches_zero_and_capacitor_supplies_load(self):
        model = Boost(47e-6, 0.0, 0.0, 0.0, 0.0, [(47e-6, 0.02)])
        samples = list(simulate_fixed_duty(
            model, vin_v=4.0, duty=0.30, load_ohm=500, fsw_hz=100_000,
            cycles=1, initial_vout_v=9.0,
        ))
        idle = [s for s in samples if s.mode == "dcm_idle"]
        self.assertEqual({s.mode for s in samples}, {"switch_on", "diode_on", "dcm_idle"})
        self.assertTrue(all(s.i_l_a >= 0 for s in samples))
        self.assertGreater(len(idle), 1)
        self.assertTrue(all(s.i_l_a == 0 for s in idle))
        self.assertGreater(idle[0].v_out_v, idle[-1].v_out_v)
        self.assertGreater(idle[-1].i_out_a, 0)
        # 轻载时输出电压近似恒定，零点约在 t_on + Vin*t_on/(Vo-Vin)。
        self.assertAlmostEqual(idle[0].t_s, 5.4e-6, delta=0.05e-6)

    def test_passive_charging_with_pwm_off(self):
        model = Boost(82e-6, 0.1, 0.03, 0.4, 0.02, [(68e-6, 0.05)])
        samples = list(simulate_fixed_duty(
            model, vin_v=4.0, duty=0.0, load_ohm=40, fsw_hz=100_000, cycles=1,
        ))
        self.assertEqual(samples[0].mode, "diode_on")
        self.assertGreater(samples[-1].i_l_a, 0)
        self.assertGreater(samples[-1].v_out_v, 0)

    def test_load_step_preserves_inductor_current_and_changes_load_current(self):
        model = Boost(82e-6, 0.1, 0.03, 0.4, 0.02, [(68e-6, 0.05)])
        samples = list(simulate_fixed_duty(
            model, vin_v=4.0, duty=0.4, load_ohm=50, fsw_hz=100_000, cycles=2,
            initial_vout_v=8, changes={1: OperatingPoint(4, 25)},
        ))
        boundary = [point for point in samples if abs(point.t_s - 1e-5) < 1e-15]
        self.assertEqual(len(boundary), 2)
        self.assertEqual(boundary[0].i_l_a, boundary[1].i_l_a)
        self.assertAlmostEqual(boundary[1].i_out_a / boundary[0].i_out_a, 2, delta=0.03)

    def test_current_sink_and_shunt_agree_with_kirchhoff(self):
        model = Boost(82e-6, 0.1, 0.03, 0.4, 0.02, [(68e-6, 0.05)])
        point = next(simulate_fixed_duty(
            model, vin_v=4, duty=0.4, load_ohm=10_000, load_current_a=0.2,
            fsw_hz=100_000, cycles=1, initial_vout_v=8, shunt_ohm=0.05,
        ))
        self.assertAlmostEqual(point.i_out_a, 0.2 + point.v_out_v / 10_000)
        self.assertAlmostEqual(point.i_out_a, 0.2008, delta=1e-5)

    def test_metrics_keep_peak_between_csv_samples(self):
        output = StringIO()
        wave = iter([
            Sample(0, 5, 1, 10, 0.2, 0.5, "switch_on"),
            Sample(50e-9, 5, 1, 13, 0.2, 0.5, "switch_on"),
            Sample(100e-9, 5, 1, 10, 0.2, 0.5, "switch_on"),
            Sample(10e-6, 5, 1, 10, 0.2, 0.5, "diode_on"),
        ])
        cycles, _, _, _ = _collect(wave, 100_000, output)
        self.assertEqual(cycles[0].v_max, 13)
        self.assertEqual(len(output.getvalue().splitlines()), 3)

    def test_independent_input_ramp_reduces_passive_inrush(self):
        model = Boost(47e-6, 0.12, 0.03, 0.4, 0.02, [(120e-6, 0.02)])
        common = dict(vin_v=5, fsw_hz=100_000, load_ohm=50_000, cycles=530)
        slow = list(simulate_input_ramp(model, slew_v_per_ms=1, **common))
        fast = simulate_fixed_duty(model, duty=0, **{**common, "cycles": 100})
        self.assertEqual(slow[0].vin_v, 0)
        self.assertEqual(slow[-1].vin_v, 5)
        self.assertTrue(all(p.duty == 0 and p.mode != "switch_on" for p in slow))
        self.assertLess(max(p.i_l_a for p in slow), max(p.i_l_a for p in fast)/4)
        self.assertGreater(slow[-1].v_out_v, 4.5)


def settings():
    return ControlSettings(
        fsw_hz=100_000, counts=100, control_stride=2, ccr_min=0, ccr_max=80,
        adc_max=4095, adc_lsb=Signals(0.01, 0.01, 0.001),
        vin_tau=10e-6, vout_tau=10e-6, csa_tau=1e-6, current_rc_tau=10e-6, shunt_ohm=0.05,
        v_set=8, i_set=1, cv_pi=(0.2, 0.1), cc_pi=(0.2, 0.1), ramp_s=0,
        ovp_sw=9, ovp_hw=10, ocp_sw=1.2, ocp_hw=2, ocp_delay_s=100e-6,
        uvlo_on=3.5, uvlo_off=3,
    )


class TestOcpStimulus(unittest.TestCase):
    def test_subset_report_preserves_full_report_and_waveforms(self):
        work = Path(__file__).resolve().parents[4] / "outputs/r6"
        req, choices, calc, selection, loss, loop = (
            load_json(work / name) for name in ("requirements.json", "design_choices.json",
                                                "calc.json", "selection.json", "losses.json", "loop.json"))
        db = load_json(KNOWLEDGE_DIR / "parts_db.json")
        rows = [Cycle(.001, 12, .25, .6, .6, 11.99, 12.01, .24, .26, .7, False)]
        protection = {"pwm_resumed_after_fault": False, "max_sw_dwell_s": 0,
                      "filtered_iout_peak_a": .25}
        # Synthetic waveform statistics exercise file isolation only, not protection success.
        with tempfile.TemporaryDirectory() as tmp, \
                patch("sim_scenarios._collect", return_value=(rows, [], None, protection)), \
                patch("sim_switching.simulate_closed_loop", return_value=iter(())), \
                patch("builtins.print"):
            dest = Path(tmp)
            full_csv = dest / "sim_S-ocp__hw_trip_nom_nominal.csv"
            full_csv.write_text("original waveform", encoding="utf-8")
            (dest / "sim.json").write_text("original full report", encoding="utf-8")
            report = run_suite(dest, req, calc, selection, choices, loop, loss, db,
                               scenario_prefix="S-ocp__hw_trip")
            self.assertEqual(full_csv.read_text(encoding="utf-8"), "original waveform")
            self.assertEqual((dest / "sim.json").read_text(encoding="utf-8"), "original full report")
            self.assertTrue((dest / "sim_subset.json").exists())
            self.assertEqual(report["scope"], "subset")
            self.assertEqual(report["cross_validation"], {})
            self.assertTrue(report["cases"])
            self.assertTrue(all(c["waveform"].startswith("sim_subset_") for c in report["cases"]))

    def test_normal_cc_load_stays_above_controllable_floor(self):
        model = Boost(82e-6, .2, .03, .4, .02, [(100e-6, .02)])
        cfg = replace(settings(), v_set=9, i_set=.5, ocp_sw=.6, ocp_hw=1)
        scenario = _ocp_stimulus(Scenario("S-ocp", 25, 36), cfg, model, 5.775, 13.55, 100_000)
        self.assertTrue(scenario.stimulus["valid"])
        # 旧 Rrated/3=6 Ω 已低于物理直通下限，不能用于“不误触发”断言。
        self.assertLess(6, (5.775-model.vf0)/cfg.i_set-model.dcr-model.rd-cfg.shunt_ohm)
        self.assertTrue(all(13.55 < r < 18 for _, r in scenario.changes))

    def test_software_stimulus_uses_passive_current_between_real_thresholds(self):
        model = Boost(82e-6, .2, .03, .4, .02, [(100e-6, .02)])
        for vin, vout in ((4.75, 12), (5.5, 9), (5.775, 9)):
            cfg = replace(settings(), v_set=vout, i_set=.5, ocp_sw=.6, ocp_hw=1)
            scenario = _ocp_stimulus(Scenario("S-ocp__sw_trip", 20, 6), cfg, model, vin, 13.55, 100_000)
            resistance = scenario.changes[-2][1]
            passive_i = (vin-model.vf0)/(resistance+model.dcr+model.rd+cfg.shunt_ohm)
            self.assertGreater(passive_i, cfg.ocp_sw)
            self.assertLess(passive_i, cfg.ocp_hw)
            self.assertGreater(scenario.changes[-1][0]-scenario.changes[-2][0], cfg.ocp_delay_s*1000)
            self.assertTrue(all(a[1] > b[1] for a, b in zip(scenario.changes[:-2], scenario.changes[1:-1])))

    def test_no_valid_cc_interval_is_not_assumed_pass(self):
        model = Boost(82e-6, .2, .03, .4, .02, [(100e-6, .02)])
        scenario = _ocp_stimulus(Scenario("S-ocp", 25, 36), settings(), model, 7.9, 9, 100_000)
        self.assertFalse(scenario.stimulus["valid"])
        _, checks, verdict = self.metrics(scenario)
        self.assertIsNone(checks["SR87"])
        self.assertEqual(verdict, "WARN")

    @staticmethod
    def metrics(scenario, fault=None):
        rows = [Cycle(.001, 8, 1, 1.1, .4, 7.99, 8.01, .99, 1.01, 1.2, False)]
        protection = {"pwm_resumed_after_fault": False, "max_sw_dwell_s": 0,
                      "filtered_iout_peak_a": 2.1 if fault else 1.0}
        return _case_metrics(scenario, rows, [], fault, protection, settings(), {}, 16, 4)

    def test_early_hardware_trip_cannot_count_as_software_delay_pass(self):
        _, checks, verdict = self.metrics(Scenario("S-ocp__sw_trip", 20, 6),
                                         {"reason": "OCP_HW", "time_s": .001})
        self.assertFalse(checks["SR87"])
        self.assertEqual(verdict, "FAIL")

    def test_uncontrolled_reference_never_counts_as_protection_pass(self):
        _, checks, verdict = self.metrics(Scenario("S-ocp__uncontrolled_reference", 20, 6))
        self.assertNotIn("SR87", checks)
        self.assertEqual(verdict, "WARN")


class TestClosedLoop(unittest.TestCase):
    def test_inactive_pi_tracks_applied_duty_after_adc_quantization(self):
        control = DigitalControl(settings())
        control.sample(0, Signals(4.0, 7.872, 0.2), applied_ccr=31)
        self.assertEqual(control.mode, "CV")
        self.assertEqual(control.pending_ccr, 4)
        self.assertAlmostEqual(control.integral["CV"], 0.1 * 0.13)
        self.assertAlmostEqual(control.integral["CC"], 0.31 - 0.2 * 0.8)

    def test_adc_at_midpulse_and_new_duty_on_next_period(self):
        cfg = settings()
        model = Boost(82e-6, 0.1, 0.03, 0.4, 0.02, [(68e-6, 0.05)])
        points = list(simulate_closed_loop(
            model, cfg, vin_v=4, load_ohm=40, cycles=4, initial_vout_v=7.7,
        ))
        reads = [point for point in points if point.adc_sample]
        period = 1 / cfg.fsw_hz
        self.assertEqual(len(reads), 2)
        self.assertEqual(reads[0].t_s, 0)
        self.assertEqual(reads[0].duty, 0)
        self.assertGreater(reads[0].pending_ccr, 0)
        first_duty = reads[0].pending_ccr / cfg.counts
        first_on = next(point for point in points if point.mode == "switch_on")
        self.assertAlmostEqual(first_on.t_s, period)
        self.assertEqual(first_on.duty, first_duty)
        self.assertAlmostEqual(reads[1].t_s, (2 + first_duty / 2) * period)
        before_update = [point for point in points if 2 * period < point.t_s < 3 * period]
        self.assertTrue(all(point.duty == first_duty for point in before_update))

    def test_analog_step_and_soft_start_reference(self):
        cfg = replace(settings(), ramp_s=1e-3)
        sense = AnalogSense(cfg)
        control = DigitalControl(cfg)
        sense.update(0, Signals(4, 6, 0.2))
        sense.update(0, Signals(4, 7, 0.2))
        v = sense.update(cfg.vout_tau, Signals(4, 7, 0.2)).vout
        self.assertAlmostEqual(v, 6.6321205588)
        control.sample(0, Signals(4, 6, 0.2), applied_ccr=0)
        self.assertEqual(control.pending_ccr, 0)
        control.sample(0.5e-3, Signals(4, 6.9, 0.2), applied_ccr=0)
        self.assertEqual(control.pending_ccr, 3)

    def test_software_overcurrent_requires_continuous_excess(self):
        control = DigitalControl(settings())
        over = Signals(4, 7, 1.3)
        control.sample(0, over, 0)
        control.sample(60e-6, Signals(4, 7, 0.8), 0)
        control.sample(100e-6, over, 0)
        control.sample(180e-6, over, 0)
        self.assertEqual(control.fault, "")
        control.sample(200e-6, over, 0)
        self.assertEqual(control.fault, "OCP_SW")
        control.sample(220e-6, Signals(4, 7, 0.8), 0)
        self.assertEqual(control.pending_ccr, 0)
        self.assertEqual(control.fault, "OCP_SW")

    def test_hardware_trip_holds_pwm_off_and_preserves_stored_current(self):
        cfg = settings()
        model = Boost(82e-6, 0.1, 0.03, 0.4, 0.02, [(68e-6, 0.05)])
        points = list(simulate_closed_loop(
            model, cfg, vin_v=4, load_ohm=40, cycles=3,
            initial_vout_v=10.5, initial_i_l_a=0.5,
        ))
        self.assertEqual(points[0].fault, "OVP_HW")
        self.assertGreater(points[0].i_l_a, 0)
        self.assertTrue(all(p.fault == "OVP_HW" and p.duty == 0 for p in points))
        self.assertNotIn("switch_on", {p.mode for p in points})

    def test_hardware_trip_interrupts_an_active_pulse(self):
        cfg = replace(settings(), ovp_sw=6.383, ovp_hw=6.398)
        model = Boost(82e-6, 0.1, 0.03, 0.4, 0.02, [(68e-6, 0.05)])
        points = list(simulate_closed_loop(
            model, cfg, vin_v=4, load_ohm=40, cycles=3,
            initial_vout_v=6.3, initial_i_l_a=1.2,
        ))
        start = next(p for p in points if p.mode == "switch_on")
        index = next(i for i, p in enumerate(points) if p.fault)
        trip, after = points[index:index + 2]
        self.assertEqual(trip.fault, "OVP_HW")
        self.assertGreater(trip.t_s, start.t_s)
        self.assertLess(trip.t_s, start.t_s + start.duty / cfg.fsw_hz)
        self.assertEqual(after.t_s, trip.t_s)
        self.assertEqual(after.i_l_a, trip.i_l_a)
        self.assertTrue(all(p.mode != "switch_on" and p.duty == 0 for p in points[index + 1:]))


if __name__ == "__main__":
    unittest.main()
