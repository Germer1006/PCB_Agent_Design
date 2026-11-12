"""非同步 Boost 开关级仿真，支持固定占空比和双 PI 闭环。

复用 Boost 模型的电路方程，分别计算 MOSFET 导通、二极管导通
和两个器件都关断的区间。单位为 V、A、Ω、F、H 和 s。
"""

import argparse
import csv
import math
from operator import mul
from pathlib import Path
from typing import NamedTuple

from _common import KNOWLEDGE_DIR, load_json, param, workdir_paths
from sim_control import AnalogSense, ControlSettings, DigitalControl, Signals
from stage_params import build, selected


class Sample(NamedTuple):
    t_s: float
    vin_v: float
    i_l_a: float
    v_out_v: float
    i_out_a: float
    duty: float
    mode: str


class ClosedLoopSample(NamedTuple):
    t_s: float
    vin_v: float
    i_l_a: float
    v_out_v: float
    i_out_a: float
    duty: float
    mode: str
    control_mode: str
    adc_sample: bool
    pending_ccr: int
    fault: str
    filtered_iout_a: float
    ocp_sw_dwell_s: float


class OperatingPoint(NamedTuple):
    vin_v: float
    load_ohm: float
    load_current_a: float = 0.0


def _rk4(derivative, x, dt):
    k1 = derivative(x)
    k2 = derivative([v + 0.5 * dt * k for v, k in zip(x, k1)])
    k3 = derivative([v + 0.5 * dt * k for v, k in zip(x, k2)])
    k4 = derivative([v + dt * k for v, k in zip(x, k3)])
    return [v + dt / 6 * (a + 2 * b + 2 * c + d)
            for v, a, b, c, d in zip(x, k1, k2, k3, k4)]


def _diode_zero(derivative, state, dt):
    """在已知跨过零点的积分步内定位二极管关断时刻。"""
    lo, hi = 0.0, dt
    for _ in range(9):
        mid = (lo + hi) / 2
        if _rk4(derivative, state, mid)[0] > 0:
            lo = mid
        else:
            hi = mid
    event = _rk4(derivative, state, hi)
    event[0] = 0.0
    return hi, event


def _step_limit(model, load_ohm):
    """ESR 支路之间的快速电荷交换可能要求步长比 50 ns 更短。"""
    g_load = 1 / load_ohm
    conductances = [1 / esr for _, esr in model.caps]
    g_total = math.fsum(conductances) + g_load
    cap_rates = [
        g * (g_load + math.fsum(conductances[:i] + conductances[i + 1:])) / (g_total * c)
        for i, ((c, _), g) in enumerate(zip(model.caps, conductances))
    ]
    inductor_rate = (model.dcr + max(model.rds, model.rd) + 1 / g_total) / model.L
    lc_rate = 1 / math.sqrt(model.L * min(c for c, _ in model.caps))
    return min(50e-9, 0.5 / max(*cap_rates, inductor_rate, lc_rate))


class _PowerStage:
    """保留连续状态，允许在采样、开关翻转和保护触发处切分积分。"""

    def __init__(self, model, vin_v, load_ohm, initial_vout_v, initial_i_l_a,
                 shunt_ohm=0.0, load_current_a=0.0):
        if not all(math.isfinite(v) for v in
                   (initial_vout_v, initial_i_l_a, shunt_ohm)):
            raise ValueError("仿真参数必须是有限数值")
        if initial_i_l_a < 0 or initial_vout_v < 0:
            raise ValueError("初始电流和电压必须 ≥ 0")
        if shunt_ohm < 0:
            raise ValueError("分流电阻必须 ≥ 0")
        if not math.isfinite(model.L) or model.L <= 0 or not model.caps:
            raise ValueError("功率级需要正电感和至少一个输出电容支路")
        if any(not math.isfinite(v) or v <= 0 for branch in model.caps for v in branch):
            raise ValueError("电容容量和 ESR 必须为正且有限")
        self.model, self.shunt_ohm = model, shunt_ohm
        self.set_operating_point(OperatingPoint(vin_v, load_ohm, load_current_a))
        initial_cap_v = initial_vout_v + (initial_vout_v / load_ohm + load_current_a) * shunt_ohm
        self.state = [initial_i_l_a] + [initial_cap_v] * len(model.caps)
        self.derivatives = {
            "switch_on": self._mosfet_on, "diode_on": self._diode_on, "dcm_idle": self._idle,
        }

    def set_operating_point(self, point):
        if (not all(map(math.isfinite, point)) or point.vin_v < 0
                or point.load_ohm <= 0 or point.load_current_a < 0):
            raise ValueError("要求 Vin、恒流负载 ≥ 0，负载电阻 > 0，且为有限数值")
        self.vin_v, self.load_ohm, self.load_current_a = point
        self.output_path_ohm = self.load_ohm + self.shunt_ohm
        self.sink_a = self.load_current_a * self.load_ohm / self.output_path_ohm
        self.limit = _step_limit(self.model, self.output_path_ohm)
        self.steps_cache = {}

    def _compiled_step(self, mode, dt):
        # 每个开关段是仿射方程。把同一步长的 RK4 合成为一次矩阵乘加。
        key = (mode, round(dt, 16))
        if key not in self.steps_cache:
            derivative = self.derivatives[mode]
            n = len(self.state)
            offset = _rk4(derivative, [0.0] * n, dt)
            columns = []
            for i in range(n):
                basis = [0.0] * n
                basis[i] = 1.0
                columns.append([v - b for v, b in zip(_rk4(derivative, basis, dt), offset)])
            self.steps_cache[key] = tuple(zip(offset, zip(*columns)))
        return self.steps_cache[key]

    def _mosfet_on(self, x):
        return self.model.f(x, 1.0, self.vin_v, self.sink_a, self.output_path_ohm)

    def _diode_on(self, x):
        return self.model.f(x, 0.0, self.vin_v, self.sink_a, self.output_path_ohm)

    def _idle(self, x):
        dx = self._mosfet_on(x)
        dx[0] = 0.0
        return dx

    def _mode(self, switch_on):
        if switch_on:
            return "switch_on"
        # iL 为零时，输入也可能经电感与二极管被动给输出电容充电。
        if self.state[0] > 0:
            return "diode_on"
        vo_with_diode_off = self.model._vo(self.state, 1.0, self.sink_a, self.output_path_ohm)
        if self.vin_v > self.model.vf0 + vo_with_diode_off:
            return "diode_on"
        return "dcm_idle"

    def _sample(self, t, duty, mode):
        d = 0.0 if mode == "diode_on" else 1.0
        cap_node = self.model._vo(self.state, d, self.sink_a, self.output_path_ohm)
        io = self.sink_a + cap_node / self.output_path_ohm
        return Sample(t, self.vin_v, self.state[0], cap_node - io * self.shunt_ohm, io, duty, mode)

    def phase(self, start, end, switch_on, duty):
        duration = end - start
        if duration <= 0:
            return
        steps = math.ceil(duration / self.limit)
        dt = duration / steps
        mode = self._mode(switch_on)
        step_map = self._compiled_step(mode, dt)
        yield self._sample(start, duty, mode)
        for index in range(steps):
            next_t = end if index + 1 == steps else start + dt * (index + 1)
            next_state = [b + sum(map(mul, row, self.state)) for b, row in step_map]
            if mode == "diode_on" and next_state[0] <= 0:
                to_zero, self.state = _diode_zero(self._diode_on, self.state, dt)
                yield self._sample(min(next_t, start + dt * index + to_zero), duty, "dcm_idle")
                next_state = _rk4(self._idle, self.state, dt - to_zero)
            self.state = next_state
            new_mode = self._mode(switch_on)
            if new_mode != mode:
                mode = new_mode
                step_map = self._compiled_step(mode, dt)
            yield self._sample(next_t, duty, mode)


def _period(fsw_hz, cycles):
    if not math.isfinite(fsw_hz) or fsw_hz <= 0:
        raise ValueError("开关频率必须为正且有限")
    if not isinstance(cycles, int) or cycles < 1:
        raise ValueError("仿真周期数必须是正整数")
    return 1 / fsw_hz


def simulate_fixed_duty(model, *, vin_v, fsw_hz, duty, load_ohm, cycles,
                        initial_vout_v=0.0, initial_i_l_a=0.0, load_current_a=0.0,
                        shunt_ohm=0.0, changes=None):
    """逐点生成固定占空比、恒阻负载的波形。PWM 翻转处保留前后两个采样。"""
    if not 0 <= duty <= 1:
        raise ValueError("占空比必须在 [0, 1] 内")
    period = _period(fsw_hz, cycles)
    stage = _PowerStage(model, vin_v, load_ohm, initial_vout_v, initial_i_l_a,
                        shunt_ohm, load_current_a)
    for cycle in range(cycles):
        if changes and cycle in changes:
            stage.set_operating_point(changes[cycle])
        start, turn_off, end = cycle * period, (cycle + duty) * period, (cycle + 1) * period
        yield from stage.phase(start, turn_off, True, duty)
        yield from stage.phase(turn_off, end, False, duty)


def simulate_input_ramp(model, *, vin_v, slew_v_per_ms, fsw_hz, load_ohm, cycles,
                        shunt_ohm=0.0):
    """独立硬件控制 Boost 输入端斜率时的冷插电，整个过程保持 PWM 关闭。"""
    if not math.isfinite(slew_v_per_ms) or slew_v_per_ms <= 0 or vin_v <= 0:
        raise ValueError("冷插电需要正的输入电压和有限的正爬升速率")
    period = _period(fsw_hz, cycles)
    stage = _PowerStage(model, 0.0, load_ohm, 0.0, 0.0, shunt_ohm)
    slope = slew_v_per_ms * 1000
    ramp_end = vin_v / slope

    def derivative(x, conducting):
        # 额外状态为实际输入电压，RK4 的各阶段使用各自的输入电压。
        dx = model.f(x[:-1], 0.0 if conducting else 1.0, x[-1], 0.0,
                     stage.output_path_ohm)
        if not conducting:
            dx[0] = 0.0
        return dx + [slope]

    def diode(x):
        return derivative(x, True)

    def idle(x):
        return derivative(x, False)

    yield stage._sample(0.0, 0.0, stage._mode(False))
    for cycle in range(cycles):
        start, end = cycle * period, (cycle + 1) * period
        stop = min(ramp_end, end)
        if start < stop:
            steps = math.ceil((stop - start) / stage.limit)
            dt = (stop - start) / steps
            for index in range(steps):
                t = stop if index + 1 == steps else start + (index + 1) * dt
                x = stage.state + [stage.vin_v]
                conducting = stage._mode(False) == "diode_on"
                y = _rk4(diode if conducting else idle, x, dt)
                if conducting and y[0] <= 0:
                    zero_t, at_zero = _diode_zero(diode, x, dt)
                    y = _rk4(idle, at_zero, dt - zero_t)
                stage.state, stage.vin_v = y[:-1], min(vin_v, slope * t)
                yield stage._sample(t, 0.0, stage._mode(False))
        if end > ramp_end:
            stage.vin_v = vin_v
            yield from stage.phase(max(start, ramp_end), end, False, 0.0)


def simulate_closed_loop(model, settings, *, vin_v, load_ohm, cycles,
                         initial_vout_v=0.0, initial_i_l_a=0.0, load_current_a=0.0,
                         initial_duty=None, changes=None):
    """闭环仿真，输入和负载可在指定 PWM 周期起点切换。"""
    period = _period(settings.fsw_hz, cycles)
    stage = _PowerStage(model, vin_v, load_ohm, initial_vout_v, initial_i_l_a,
                        settings.shunt_ohm, load_current_a)
    control = DigitalControl(settings)
    if initial_duty is not None:
        control.prime(initial_duty)
    analog = AnalogSense(settings)
    for cycle in range(cycles):
        if changes and cycle in changes:
            stage.set_operating_point(changes[cycle])
        applied_ccr = control.pending_ccr
        duty = applied_ccr / settings.counts
        start, turn_off, end = cycle * period, (cycle + duty) * period, (cycle + 1) * period
        adc_at = (start + turn_off) / 2 if cycle % settings.control_stride == 0 else None
        boundaries = sorted({start, turn_off, end} | ({adc_at} if adc_at is not None else set()))
        sampled = False
        for left, right in zip(boundaries, boundaries[1:]):
            while left < right:
                gate_on = left < turn_off and not control.fault
                for raw in stage.phase(left, right, gate_on, 0.0 if control.fault else duty):
                    sense = analog.update(raw.t_s, Signals(raw.vin_v, raw.v_out_v, raw.i_out_a))
                    control.check_hardware(raw.t_s, sense)
                    sample_now = not sampled and raw.t_s == adc_at
                    if sample_now:
                        control.sample(raw.t_s, sense, applied_ccr)
                        sampled = True
                    dwell = control.ocp_observed_dwell_s if sample_now else 0.0
                    yield ClosedLoopSample(*raw, control.mode, sample_now,
                                           control.pending_ccr, control.fault, sense.iout, dwell)
                    if gate_on and control.fault:
                        # 电感和电容状态保留，在故障时刻切换到关断段。
                        left = raw.t_s
                        break
                else:
                    break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workdir", type=Path, help="含参数计算和选型结果的工作目录")
    parser.add_argument("--db", type=Path, default=KNOWLEDGE_DIR / "parts_db.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--duty", type=float, help="固定占空比，0–1")
    mode.add_argument("--closed-loop", action="store_true", help="使用设计输出的数字控制参数")
    parser.add_argument("--load-ohm", type=float, help="单工况模式的负载电阻，Ω")
    parser.add_argument("--cycles", type=int, default=200, help="仿真的 PWM 周期数")
    parser.add_argument("--vin-v", type=float, help="输入电压，默认使用需求的标称值")
    parser.add_argument("--initial-vout-v", type=float, default=0.0, help="输出电容初始电压")
    parser.add_argument("--output", type=Path, help="CSV 输出路径，默认写入工作目录")
    args = parser.parse_args()
    paths = workdir_paths(args.workdir)
    req, choices, calc, selection = (
        load_json(paths[k]) for k in ("requirements", "choices", "calc", "selection")
    )
    db = load_json(args.db)
    if args.duty is None and not args.closed_loop:
        from sim_scenarios import run_suite

        report = run_suite(args.workdir, req, calc, selection, choices,
                           load_json(paths["loop"]), load_json(paths["losses"]), db)
        print(f"T1 完成：{len(report['cases'])} 个工况，{report['elapsed_s']:g} s；详见 sim.json")
        return
    if args.load_ohm is None:
        parser.error("单工况模式需要 --load-ohm")
    model, _ = build(selection, db, calc, choices)
    common = dict(vin_v=req["vin"]["nom_v"] if args.vin_v is None else args.vin_v,
                  load_ohm=args.load_ohm, cycles=args.cycles, initial_vout_v=args.initial_vout_v)
    if args.closed_loop:
        csa, _ = selected(selection, db, "csa")
        bandwidth = param(csa, "bandwidth_khz", ("typ", "max")) if csa is not None else None
        if bandwidth is None:
            parser.error("闭环仿真需要所选电流放大器的带宽")
        settings = ControlSettings.from_design(calc, load_json(paths["loop"]), choices, bandwidth * 1000)
        wave = simulate_closed_loop(model, settings, **common)
        filename = "sim_closed_loop.csv"
    else:
        wave = simulate_fixed_duty(model, fsw_hz=choices["fsw_hz"], duty=args.duty, **common)
        filename = "sim_open_loop.csv"
    output = args.output or args.workdir / filename
    first = next(wave)
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(first._fields)
        writer.writerow(first)
        writer.writerows(wave)
    print(f"仿真波形已写入 {output}")


if __name__ == "__main__":
    main()
