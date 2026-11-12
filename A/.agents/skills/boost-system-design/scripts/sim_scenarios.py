"""T1 场景矩阵、原始波形统计及规则判据。"""

import csv
import math
import time
from dataclasses import dataclass
from html import escape
from typing import NamedTuple

from _common import param, save_json
from boost_model import Boost
from sim_control import ControlSettings
from stage_params import selected, variants


@dataclass(frozen=True)
class Scenario:
    name: str
    duration_ms: float
    load_ohm: float
    load_a: float = 0.0
    target: str = "CV"
    changes: tuple = ()


class Cycle(NamedTuple):
    t: float
    v: float
    i: float
    il: float
    duty: float
    v_min: float
    v_max: float
    i_min: float
    i_max: float
    il_max: float
    dcm: bool


def _collect(wave, fsw, stream):
    """先用全部积分点统计，再把约 4 点/周期和故障边沿写入 CSV。"""
    writer = csv.writer(stream)
    previous = next(wave)
    writer.writerow(previous._fields)
    writer.writerow(previous)
    written, index = previous.t_s, 0
    cycles, control, fault = [], [], None
    peak_filtered_iout = max_sw_dwell_s = 0.0
    pwm_resumed_after_fault = False
    dt_sum = sv = si = sil = sd = 0.0
    lo_v = lo_i = math.inf
    hi_v = hi_i = hi_il = -math.inf
    dcm = False
    for point in _points_with_first(previous, wave):
        t, vin, il, v, i, d, mode = point[:7]
        if hasattr(point, "adc_sample") and point.adc_sample:
            control.append((t, point.control_mode))
            max_sw_dwell_s = max(max_sw_dwell_s, point.ocp_sw_dwell_s)
        peak_filtered_iout = max(peak_filtered_iout, getattr(point, "filtered_iout_a", 0.0))
        new_fault = fault is None and getattr(point, "fault", "")
        if new_fault:
            fault = {"time_s": t, "reason": point.fault}
        elif fault and t > fault["time_s"] and mode == "switch_on":
            pwm_resumed_after_fault = True
        if t - written >= 0.25 / fsw or new_fault:
            writer.writerow(point)
            written = t
        dt = t - previous.t_s
        if dt > 0:
            bucket = int((t + previous.t_s) * 0.5 * fsw)
            if bucket != index:
                cycles.append(Cycle(previous.t_s, sv/dt_sum, si/dt_sum, sil/dt_sum, sd/dt_sum,
                                    lo_v, hi_v, lo_i, hi_i, hi_il, dcm))
                index, dt_sum = bucket, 0.0
                sv = si = sil = sd = 0.0
                lo_v = lo_i = math.inf
                hi_v = hi_i = hi_il = -math.inf
                dcm = False
            dt_sum += dt
            half = dt * 0.5
            sv += (previous.v_out_v + v) * half
            si += (previous.i_out_a + i) * half
            sil += (previous.i_l_a + il) * half
            sd += previous.duty * dt
            lo_v, hi_v = min(lo_v, previous.v_out_v, v), max(hi_v, previous.v_out_v, v)
            lo_i, hi_i = min(lo_i, previous.i_out_a, i), max(hi_i, previous.i_out_a, i)
            hi_il = max(hi_il, previous.i_l_a, il)
            dcm = dcm or previous.mode == "dcm_idle"
        previous = point
    if dt_sum:
        cycles.append(Cycle(previous.t_s, sv/dt_sum, si/dt_sum, sil/dt_sum, sd/dt_sum,
                            lo_v, hi_v, lo_i, hi_i, hi_il, dcm))
    if previous.t_s > written:
        writer.writerow(previous)
    if not cycles or not all(math.isfinite(value) for row in cycles for value in row[:-1]):
        raise ValueError("仿真未产生有限的波形统计")
    protection = {"filtered_iout_peak_a": peak_filtered_iout,
                  "max_sw_dwell_s": max_sw_dwell_s,
                  "pwm_resumed_after_fault": pwm_resumed_after_fault}
    return cycles, control, fault, protection


def _points_with_first(first, rest):
    yield first
    yield from rest


def _mean(rows, key):
    return sum(getattr(r, key) for r in rows) / len(rows)


def _settled(rows, start, reference, field="v"):
    """返回最后一次超出 ±2% 后、余下窗口一直在带内的时刻。"""
    last_bad = None
    for index, row in enumerate(rows):
        if abs(getattr(row, field) - reference) > 0.02 * reference:
            last_bad = index
    if last_bad is None:
        return 0.0
    if last_bad == len(rows) - 1:
        return None
    return 1000 * (rows[last_bad + 1].t - start)


def _wave_svg(traces, path):
    width, panel = 780, 138
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{45+panel*len(traces)}" '
           'font-family="Microsoft YaHei, Arial" font-size="12">',
           '<rect width="100%" height="100%" fill="white"/>',
           '<text x="390" y="22" text-anchor="middle" font-size="16">T1 开关级仿真（每周期平均值）</text>']
    for index, (label, rows, field, unit) in enumerate(traces):
        top = 42 + panel * index
        values = [getattr(r, field) for r in rows]
        lo, hi = min(values), max(values)
        pad = max((hi - lo) * .08, .001)
        lo, hi = lo - pad, hi + pad
        points = " ".join(f"{62+690*r.t/rows[-1].t:.1f},{top+104-84*(v-lo)/(hi-lo):.1f}"
                          for r, v in zip(rows, values))
        out.extend([f'<text x="62" y="{top+12}">{escape(label)} · {unit}</text>',
                    f'<rect x="62" y="{top+20}" width="690" height="84" fill="none" stroke="#999"/>',
                    f'<text x="6" y="{top+27}">{hi:.2f}</text><text x="6" y="{top+104}">{lo:.2f}</text>',
                    f'<polyline points="{points}" fill="none" stroke="#2563eb" stroke-width="1.4"/>',
                    f'<text x="62" y="{top+121}">0</text><text x="695" y="{top+121}">{rows[-1].t*1000:g} ms</text>'])
    out.append("</svg>")
    path.write_text("\n".join(out), encoding="utf-8")


def _case_metrics(scenario, rows, control, fault, protection, settings, req, tvs_v, isat):
    v_set, i_set = settings.v_set, settings.i_set
    t_end = rows[-1].t
    tail = [r for r in rows if r.t > t_end - 0.003]
    v_avg, i_avg = _mean(tail, "v"), _mean(tail, "i")
    v_peak, il_peak = max(r.v_max for r in rows), max(r.il_max for r in rows)
    vpp = max(r.v_max - r.v_min for r in tail)
    metrics = {
        "v_avg_v": v_avg, "i_avg_a": i_avg, "il_avg_a": _mean(tail, "il"),
        "duty_avg": _mean(tail, "duty"), "v_peak_v": v_peak, "il_peak_a": il_peak,
        "i_peak_a": max(r.i_max for r in rows),
        "v_error_pct": 100 * (v_avg/v_set - 1), "i_error_pct": 100 * (i_avg/i_set - 1),
        "ripple_vpp_v": vpp, "ripple_pct": 100*vpp/v_set,
        "dcm": any(r.dcm for r in tail), "fault": fault,
    }
    name = scenario.name.split("__", 1)[0]
    checks = {}
    if name == "S-start":
        at_10 = next((r.t for r in rows if r.v >= .1*v_set), None)
        at_90 = next((r.t for r in rows if r.v >= .9*v_set), None)
        overshoot = max(0, 100*(v_peak/v_set - 1))
        settle = _settled(rows, 0, v_set)
        metrics.update(rise_ms=(at_90 - at_10)*1000 if at_90 is not None else None,
                       settle_ms=settle, overshoot_pct=overshoot)
        checks["SR80"] = overshoot < 10 and fault is None and settle is not None
    if name in {"S-steady-CV", "S-rated", "S-ripple-corner"}:
        checks["SR81"] = abs(metrics["v_error_pct"]) < req["vout"]["steady_err_pct"] and fault is None
        checks["SR82"] = metrics["ripple_pct"] < req["ripple"]["max_pct"]
        checks["SR82-target"] = metrics["ripple_pct"] < req["ripple"]["target_pct"]
    if name == "S-steady-CC":
        checks["SR81"] = abs(metrics["i_error_pct"]) < req["iout"]["steady_err_pct"] and fault is None
    if name in {"S-step", "S-handover"}:
        phases = []
        start_ms = 0
        for end_ms, target in ((10, "CV"), (20, "CC" if name == "S-handover" else "CV"), (30, "CV")):
            window = [r for r in rows if start_ms/1000 < r.t <= end_ms/1000 + 1e-12]
            end_modes = [m for t, m in control if (end_ms - 3)/1000 < t < end_ms/1000]
            active = max(("CV", "CC"), key=end_modes.count) if end_modes else None
            end_window = [r for r in window if r.t > (end_ms - 3)/1000]
            field, reference = ("i", i_set) if target == "CC" else ("v", v_set)
            envelope = 100 * (max(getattr(r, field) for r in end_window)
                              - min(getattr(r, field) for r in end_window)) / reference
            phases.append({"start_ms": start_ms, "target_mode": target, "tail_dominant": active,
                           "settle_ms": _settled(window, start_ms/1000, reference, field),
                           "tail_envelope_pct": envelope,
                           "v_peak_v": max(r.v_max for r in window), "v_min_v": min(r.v_min for r in window),
                           "i_peak_a": max(r.i_max for r in window),
                           "v_overshoot_pct": max(0, 100 * (max(r.v_max for r in window)/v_set - 1)),
                           "v_undershoot_pct": max(0, 100 * (1 - min(r.v_min for r in window)/v_set))})
            start_ms = end_ms
        metrics["phases"] = phases
        metrics["mode_transitions"] = [{"time_s": t, "mode": m} for j, (t, m) in enumerate(control)
                                       if not j or m != control[j-1][1]]
        if name == "S-handover":
            checks["SR83"] = fault is None and all(
                p["tail_dominant"] == p["target_mode"] and p["tail_envelope_pct"] <= 1
                and p["settle_ms"] is not None for p in phases)
    if name == "S-light":
        envelope = 100 * (max(r.v_max for r in tail) - min(r.v_min for r in tail)) / v_set
        metrics["tail_envelope_pct"] = envelope
        checks["SR84"] = (envelope <= 1 and fault is None
                          and abs(metrics["v_error_pct"]) < req["vout"]["steady_err_pct"])
    if name == "S-ovp":
        metrics.update(tvs_vbr_min_v=tvs_v, ovp_hw_v=settings.ovp_hw,
                       ovp_action_observed=bool(fault and fault["reason"].startswith("OVP")))
        checks["SR85"] = v_peak < tvs_v if tvs_v is not None else None
    if name in {"S-ocp", "S-plugin"}:
        metrics.update(inductor_isat_a=isat, linear_model_beyond_isat=il_peak >= isat if isat else None)
        if name == "S-ocp":
            expected = {"sw_trip": "OCP_SW", "hw_trip": "OCP_HW"}.get(
                scenario.name.partition("__")[2], "")
            actual = fault["reason"] if fault else ""
            observed = actual.startswith("OCP")
            metrics.update(ocp_action_observed=observed, ocp_sw_a=settings.ocp_sw,
                           ocp_hw_a=settings.ocp_hw, ocp_sw_delay_ms=settings.ocp_delay_s*1000,
                           expected_fault=expected or None, **protection)
            action_ok = (actual == expected and not protection["pwm_resumed_after_fault"])
            if expected == "OCP_SW":
                max_interval = 2 * settings.control_stride / settings.fsw_hz
                action_ok = action_ok and (settings.ocp_delay_s - 1e-12 <= protection["max_sw_dwell_s"]
                                          <= settings.ocp_delay_s + max_interval)
            elif expected == "OCP_HW":
                action_ok = action_ok and protection["filtered_iout_peak_a"] >= settings.ocp_hw
            else:
                action_ok = action_ok and protection["filtered_iout_peak_a"] < settings.ocp_hw \
                    and protection["max_sw_dwell_s"] < settings.ocp_delay_s
            checks["SR87"] = action_ok and il_peak < isat if isat else None
            if observed:
                fault_ms = fault["time_s"]*1000
                step_ms = max((ms for ms, _ in scenario.changes if ms <= fault_ms), default=0)
                metrics["ocp_action_ms_from_load_step"] = fault_ms - step_ms
        if name == "S-plugin":
            metrics["inrush_peak_time_ms"] = max(rows, key=lambda r: r.il_max).t * 1000
    if any(result is False and key != "SR82-target" for key, result in checks.items()):
        verdict = "FAIL"
    elif (metrics.get("linear_model_beyond_isat") or any(result is None or result is False for result in checks.values())
          or (name == "S-step" and (fault or any(p["settle_ms"] is None for p in metrics["phases"])))):
        verdict = "WARN"
    else:
        verdict = "PASS"
    return metrics, checks, verdict


def run_suite(workdir, req, calc, selection, choices, loop, loss, db):
    from sim_switching import OperatingPoint, simulate_closed_loop, simulate_fixed_duty, simulate_input_ramp

    start_time = time.perf_counter()
    csa, _ = selected(selection, db, "csa")
    tvs, _ = selected(selection, db, "tvs")
    inductor, _ = selected(selection, db, "inductor")
    bandwidth = param(csa, "bandwidth_khz", ("typ", "max")) if csa else None
    if not bandwidth:
        raise ValueError("T1 需要所选电流放大器的带宽")
    settings = ControlSettings.from_design(calc, loop, choices, bandwidth*1000)
    tvs_v = param(tvs, "vbr_min_v", ("min", "typ")) if tvs else None
    isat = param(inductor, "isat_a", ("min", "typ")) if inductor else None
    model_set = {name: model for name, model, _ in variants(selection, db, calc, choices)}
    vi = calc["inputs"]["vin"]
    v_set, i_set, fsw = settings.v_set, settings.i_set, settings.fsw_hz
    rated_r = v_set/i_set
    divider = calc["sensing"]["vout"]
    bleed = divider["r_top_ohm"] + divider["r_bot_ohm"]
    parallel = lambda r: 1/(1/r + 1/bleed) if r else bleed
    ramp = choices["protection"].get("input_ramp")
    if ramp:
        slew, source_budget = ramp["max_slew_v_per_ms"], ramp["source_peak_budget_a"]
        if not all(math.isfinite(x) and x > 0 for x in (slew, source_budget)):
            raise ValueError("独立输入缓升的速率与源电流预算必须为正且有限")
        lc = choices["losses"]
        cin_uf = 0.0
        for role, tolerance in (("cin_ceramic", lc["ceramic_cap_tolerance"]),
                                ("cin_bulk", lc["bulk_cap_tolerance"])):
            part, count = selected(selection, db, role)
            capacitance = param(part, "capacitance_uf") if part else None
            if not capacitance:
                raise ValueError(f"独立输入缓升需要 {role} 电容上界")
            cin_uf += capacitance * count * (1 + tolerance)
        # 用全标称陶瓷电容及其容差模拟 0–5 V，而非 12 V 偏压降容。
        ceramic, _ = selected(selection, db, "cout_ceramic")
        if not ceramic:
            raise ValueError("冷插电仿真需要已选输出陶瓷电容")
        retention = ceramic.get("bias_retention_12v") or choices["power_stage"]["cout_ceramic_bias_retention"]
        cold_models = {
            name: Boost(m.L, m.dcr, m.rds, m.vf0, m.rd,
                        [(m.caps[0][0] / retention, m.caps[0][1])] + m.caps[1:])
            for name, m in model_set.items()
        }
    def current_case(name, value, duration):
        return Scenario(name, duration, bleed, value)

    steady_cc = [max(calc["cc_range"]["r_min_ohm"], rated_r*ratio)
                 for ratio in (.52, .75, 11/12)]
    scenarios = [Scenario("S-start", 40, parallel(rated_r))]
    scenarios += [current_case(f"S-steady-CV__{label}", i_set*scale, 8)
                  for label, scale in (("low", .2), ("mid", .5), ("high", .9))]
    scenarios += [Scenario(f"S-steady-CC__{n+1}", 8, parallel(r), target="CC")
                  for n, r in enumerate(steady_cc)]
    scenarios += [
        Scenario("S-step", 30, parallel(2*rated_r),
                 changes=((10, parallel(rated_r)), (20, parallel(2*rated_r)))),
        Scenario("S-handover", 30, parallel(1.25*rated_r),
                 changes=((10, parallel(rated_r*2/3)), (20, parallel(1.25*rated_r)))),
        current_case("S-light__low", .04*i_set, 12),
        current_case("S-light__no_load", 0, 12),
        Scenario("S-ovp", 15, parallel(rated_r*1.25), changes=((5, bleed),)),
        Scenario("S-ocp", 20 + settings.ocp_delay_s*1000, parallel(2*rated_r),
                 changes=((5, parallel(rated_r)), (10, parallel(rated_r*2/3)),
                          (15, parallel(rated_r/3)))),
        Scenario("S-ocp__hw_trip", 10, parallel(2*rated_r),
                 changes=((2, parallel(rated_r/3)), (5, parallel(2*rated_r)))),
        Scenario("S-ocp__sw_trip", 12 + settings.ocp_delay_s*1000, parallel(rated_r/3),
                 target="CC", changes=((3, parallel(rated_r/4)),
                                       (9 + settings.ocp_delay_s*1000, parallel(2*rated_r)))),
        Scenario("S-plugin__protected" if ramp else "S-plugin", vi["max"]/slew + 2 if ramp else 2, bleed),
    ]
    cases = [(s, "nominal", key) for s in scenarios for key in ("min", "nom", "max")]
    worst_eff, worst_eff_vin = loss["envelope"]["eta_min_case"].split("/")
    worst_ripple, worst_ripple_vin = loss["ripple"]["case"].split("/")
    worst_inrush = loss["inrush"]["variant"]
    for name, var, key in (("S-start", worst_eff, worst_eff_vin),
                           ("S-handover", worst_eff, worst_eff_vin),
                           ("S-light__no_load", worst_ripple, worst_ripple_vin),
                           ("S-ovp", worst_ripple, worst_ripple_vin),
                           ("S-ocp", worst_eff, worst_eff_vin),
                           ("S-ocp__hw_trip", worst_eff, worst_eff_vin),
                           ("S-ocp__sw_trip", worst_eff, worst_eff_vin),
                           ("S-plugin__protected" if ramp else "S-plugin", worst_inrush, "max")):
        item = (next(s for s in scenarios if s.name == name), var, key)
        if item not in cases:
            cases.append(item)
    if ramp:
        protected = next(s for s in scenarios if s.name == "S-plugin__protected")
        cases += [(protected, var, "max") for var in model_set if (protected, var, "max") not in cases]
        direct = Scenario("S-plugin__direct_reference", 2, bleed)
        cases += [(direct, "nominal", key) for key in ("min", "nom", "max")]
        if worst_inrush != "nominal":
            cases.append((direct, worst_inrush, "max"))
    cases += [(Scenario("S-rated", 8, parallel(rated_r)), "nominal", "nom"),
              (Scenario("S-ripple-corner", 8, parallel(rated_r)), worst_ripple, worst_ripple_vin)]

    traces, results, references = [], [], []
    for scenario, variant, key in cases:
        case_start = time.perf_counter()
        model, vin = model_set[variant], vi[key]
        total_cycles = math.ceil(scenario.duration_ms*fsw/1000 - 1e-12)
        common = dict(vin_v=vin, load_ohm=scenario.load_ohm,
                      cycles=total_cycles, load_current_a=scenario.load_a)
        changes = {round(ms*fsw/1000): OperatingPoint(vin, r) for ms, r in scenario.changes}
        if scenario.name == "S-plugin__protected":
            wave = simulate_input_ramp(cold_models[variant], vin_v=vin, slew_v_per_ms=slew,
                                       fsw_hz=fsw, load_ohm=bleed, cycles=total_cycles,
                                       shunt_ohm=settings.shunt_ohm)
        elif scenario.name.startswith("S-plugin"):
            common["vin_v"] = 0.0
            wave = simulate_fixed_duty(model, fsw_hz=fsw, duty=0, **common,
                                       shunt_ohm=settings.shunt_ohm,
                                       changes={1: OperatingPoint(vin, bleed)})
        elif scenario.name == "S-start":
            wave = simulate_closed_loop(model, settings, **common)
        else:
            vo = i_set * scenario.load_ohm if scenario.target == "CC" else v_set
            io = vo/scenario.load_ohm + scenario.load_a
            cap_v = vo + io * settings.shunt_ohm
            point = model.solve_dc(vin, cap_v, io)
            duty, il = point if point else (0.0, 0.0)
            valley = il - max(0, vin - il*(model.dcr + model.rds))*duty/(2*fsw*model.L)
            if point and valley < 0:
                duty = math.sqrt(2*model.L*fsw*io*max(0, cap_v + model.vf0 - vin))/vin
            wave = simulate_closed_loop(model, settings, **common, changes=changes,
                                        initial_vout_v=vo, initial_i_l_a=max(0, valley), initial_duty=duty)

        label = f"{scenario.name}_{key}_{variant}"
        filename = f"sim_{label}.csv"
        with (workdir / filename).open("w", encoding="utf-8", newline="") as stream:
            rows, mode_history, fault, protection = _collect(wave, fsw, stream)
        metrics, checks, verdict = _case_metrics(scenario, rows, mode_history, fault, protection,
                                                settings, req, tvs_v, isat)
        if scenario.name == "S-plugin__protected":
            # 用最大 Cin 充电电流和辅助电流保守叠加，而非把电感电流当作源电流。
            auxiliary = calc["aux"]["p_aux_w"] / vi["min"]
            source_upper = metrics["il_peak_a"] + cin_uf * slew * 1e-3 + auxiliary
            metrics.update(input_slew_v_per_ms=slew, input_cap_upper_uf=cin_uf,
                           source_peak_bound_a=source_upper, source_peak_budget_a=source_budget,
                           pwm_held_off=all(r.duty == 0 for r in rows))
            checks["SR88"] = (metrics["il_peak_a"] < isat and source_upper <= source_budget
                              and metrics["pwm_held_off"]) if isat else None
            verdict = {True: "PASS", False: "FAIL", None: "WARN"}[checks["SR88"]]
        elif scenario.name.startswith("S-plugin"):
            ref = next((entry for entry in loss["inrush_cases"] if entry["variant"] == variant), None)
            if ref:
                metrics["linear_inrush_at_vin_max_a"] = ref["i_peak_a"]
                metrics["linear_reference_vin_v"] = vi["max"]
                if key == "max":
                    metrics["inrush_error_vs_linear_pct"] = 100 * (metrics["il_peak_a"]/ref["i_peak_a"] - 1)
        record = {"name": scenario.name, "vin_v": vin, "vin_case": key, "variant": variant,
                  "waveform": filename, "verdict": verdict, "checks": checks, "metrics": metrics,
                  "elapsed_s": round(time.perf_counter() - case_start, 2),
                  "profile": {"duration_ms": scenario.duration_ms, "initial_load_ohm": scenario.load_ohm,
                              "cc_load_a": scenario.load_a, "load_steps_ms_ohm": scenario.changes}}
        (references if scenario.name == "S-plugin__direct_reference" else results).append(record)
        if key == "nom" and variant == "nominal" and scenario.name in {
            "S-start", "S-step", "S-handover", "S-light__no_load", "S-plugin",
            "S-plugin__protected", "S-plugin__direct_reference",
        }:
            field, unit = ("il", "A") if scenario.name.startswith("S-plugin") else ("v", "V")
            traces.append((scenario.name, rows, field, unit))
        print(f"T1 {label}: {verdict}", flush=True)

    rated = next(case["metrics"] for case in results if case["name"] == "S-rated")
    ripple_corner = next(case["metrics"] for case in results if case["name"] == "S-ripple-corner")
    nominal = loss["cases"]["nom"]
    cross = {}
    for name, actual, expected, limit in (
        ("duty", rated["duty_avg"], nominal["D"], 2),
        ("il_avg", rated["il_avg_a"], nominal["IL_avg_A"], 2),
        ("ripple_mv", 1000*rated["ripple_vpp_v"], nominal["vpp_harmonic_mv"], 20),
        ("worst_ripple_mv", 1000*ripple_corner["ripple_vpp_v"], loss["ripple"]["vpp_harmonic_mv"], 20),
    ):
        error = abs(actual/expected - 1)*100
        cross[name] = {"simulated": actual, "reference": expected, "error_pct": error,
                       "limit_pct": limit, "passed": error < limit}
    _wave_svg(traces, workdir / "sim_waveforms.svg")
    report = {
        "schema": "boost-switching-sim/1", "revision": choices["revision"],
        "cross_validation": cross, "cases": results, "reference_cases": references,
        "input_ramp": ramp,
        "elapsed_s": round(time.perf_counter() - start_time, 2),
        "model_limits": ["理想开关沿，忽略布线寄生电感及温升动态",
                         "CC 电子负载假定已进入其工作电压范围",
                         "比较器按积分步采样，阈值与时延仍需实测",
                         "超过 Isat 的电流为线性模型结果，不是可靠故障电流上界",
                         "输入缓升假定由独立硬件在 Boost 输入端实现；实物电源、器件 SOA、容差和 PWM 使能仍须验证"],
    }
    save_json(workdir / "sim.json", report)
    return report
