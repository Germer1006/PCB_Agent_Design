"""T1 数字控制器和采样链。输入使用生成固件头文件的同一组设计数据。"""

import math
from dataclasses import dataclass
from typing import NamedTuple


class Signals(NamedTuple):
    vin: float
    vout: float
    iout: float


@dataclass(frozen=True)
class ControlSettings:
    fsw_hz: float
    counts: int
    control_stride: int
    ccr_min: int
    ccr_max: int
    adc_max: int
    adc_lsb: Signals
    vin_tau: float
    vout_tau: float
    csa_tau: float
    current_rc_tau: float
    shunt_ohm: float
    v_set: float
    i_set: float
    cv_pi: tuple
    cc_pi: tuple
    ramp_s: float
    ovp_sw: float
    ovp_hw: float
    ocp_sw: float
    ocp_hw: float
    ocp_delay_s: float
    uvlo_on: float
    uvlo_off: float

    @classmethod
    def from_design(cls, calc, loop, choices, csa_bandwidth_hz):
        pwm, timing = calc["pwm"], calc["timing"]
        sense, protect = calc["sensing"], calc["protection"]
        if pwm["alignment"] != "edge" or choices["control"]["structure"] != "dual_pi_min_select":
            raise ValueError("T1 支持边沿对齐 PWM 和双 PI 取小控制")
        if {calc["revision"], loop["revision"], choices["revision"]} != {choices["revision"]}:
            raise ValueError("仿真输入的计算、环路与设计决策版本不一致")
        if any(not loop[mode]["ok"] for mode in ("CV", "CC")):
            raise ValueError("闭环仿真需要 CV 与 CC 环路的有效 PI 参数")
        if csa_bandwidth_hz <= 0:
            raise ValueError("需要所选电流放大器的正带宽参数")

        fsw = calc["inputs"]["fsw_hz"]
        counts = pwm["arr"] + 1
        stride = timing["tim1_rcr"] + 1
        if stride < 1 or not math.isclose(fsw / stride, timing["f_ctrl_hz"]):
            raise ValueError("PWM 与控制频率应满足 RCR + 1 的整数周期关系")
        adc_max = 2 ** sense["adc_bits"] - 1
        adc_v_per_code = sense["adc_vref_v"] / adc_max
        limits = protect["duty_limit"]
        ccr_min, ccr_max = math.ceil(limits["dmin"] * counts), limits["ccr_max"]
        if not 0 <= ccr_min <= ccr_max < counts:
            raise ValueError("PWM 占空比限幅无效")

        return cls(
            fsw_hz=fsw, counts=counts, control_stride=stride,
            ccr_min=ccr_min, ccr_max=ccr_max, adc_max=adc_max,
            adc_lsb=Signals(adc_v_per_code / sense["vin"]["ratio"],
                            adc_v_per_code / sense["vout"]["ratio"],
                            adc_v_per_code / sense["iout"]["transimpedance_v_per_a"]),
            vin_tau=1 / (2 * math.pi * sense["vin"]["filter_fc_hz"]),
            vout_tau=1 / (2 * math.pi * sense["vout"]["filter_fc_hz"]),
            csa_tau=1 / (2 * math.pi * csa_bandwidth_hz),
            current_rc_tau=choices["losses"]["current_filter_r_ohm"]
            * choices["losses"]["current_filter_c_f"],
            shunt_ohm=sense["iout"]["shunt_ohm"],
            v_set=calc["inputs"]["vout_v"], i_set=calc["inputs"]["iout_cc_a"],
            cv_pi=(loop["CV"]["Kp"], loop["CV"]["Ki_Tc"]),
            cc_pi=(loop["CC"]["Kp"], loop["CC"]["Ki_Tc"]),
            ramp_s=protect["soft_start"]["ramp_ms"] / 1000,
            ovp_sw=protect["ovp_sw"]["value"], ovp_hw=protect["ovp_hw"]["value"],
            ocp_sw=protect["ocp_sw"]["value"], ocp_hw=protect["ocp_hw"]["value"],
            ocp_delay_s=protect["ocp_sw"]["delay_ms"] / 1000,
            uvlo_on=protect["uvlo"]["on_v"], uvlo_off=protect["uvlo"]["off_v"],
        )


def _pole(previous, drive, dt, tau):
    return previous + (drive - previous) * -math.expm1(-dt / tau)


class AnalogSense:
    """在功率级积分步推进分压 RC 及电流放大器、RC 两级滤波。"""

    def __init__(self, settings):
        self.settings = settings
        self.t = None
        self.raw = None
        self.filtered = None
        self.csa = None

    def update(self, t, raw):
        if self.t is None:
            self.t, self.raw, self.filtered, self.csa = t, raw, raw, raw.iout
            return raw
        dt = t - self.t
        if dt > 0:
            s = self.settings
            old_csa = self.csa
            self.csa = _pole(old_csa, (self.raw.iout + raw.iout) / 2, dt, s.csa_tau)
            self.filtered = Signals(
                _pole(self.filtered.vin, (self.raw.vin + raw.vin) / 2, dt, s.vin_tau),
                _pole(self.filtered.vout, (self.raw.vout + raw.vout) / 2, dt, s.vout_tau),
                _pole(self.filtered.iout, (old_csa + self.csa) / 2, dt, s.current_rc_tau),
            )
        self.t, self.raw = t, raw
        return self.filtered


class DigitalControl:
    def __init__(self, settings):
        self.settings = settings
        self.pending_ccr = 0
        self.integral = {"CV": 0.0, "CC": 0.0}
        self.mode = "STANDBY"
        self.fault = ""
        self.fault_time_s = None
        self.start_s = None
        self.start_v = None
        self.overcurrent_since = None
        self.ocp_observed_dwell_s = 0.0

    def _compare_value(self, duty):
        s = self.settings
        return min(s.ccr_max, max(s.ccr_min, math.floor(duty * s.counts + 0.5)))

    def prime(self, duty):
        """把稳态和阶跃场景初始化在已启用的工作点。"""
        self.pending_ccr = self._compare_value(duty)
        self.start_s, self.start_v = -self.settings.ramp_s, self.settings.v_set
        self.integral = dict.fromkeys(self.integral, self.pending_ccr / self.settings.counts)

    def _latch(self, fault, t):
        self.fault, self.fault_time_s = fault, t
        self.mode, self.pending_ccr = "FAULT", 0

    def check_hardware(self, t, sense):
        if self.fault:
            return
        if sense.vout >= self.settings.ovp_hw:
            self._latch("OVP_HW", t)
        elif sense.iout >= self.settings.ocp_hw:
            self._latch("OCP_HW", t)

    def sample(self, t, sense, applied_ccr):
        self.ocp_observed_dwell_s = 0.0
        if self.fault:
            return
        s = self.settings
        if any(not math.isfinite(value) for value in sense):
            self._latch("ADC_INVALID", t)
            return
        codes = [max(0, min(s.adc_max, math.floor(value / lsb + 0.5)))
                 for value, lsb in zip(sense, s.adc_lsb)]
        measured = Signals(*(code * lsb for code, lsb in zip(codes, s.adc_lsb)))
        if s.adc_max in codes:
            self._latch("ADC_SATURATED", t)
            return
        if measured.vout >= s.ovp_sw:
            self._latch("OVP_SW", t)
            return
        if measured.iout >= s.ocp_sw:
            if self.overcurrent_since is None:
                self.overcurrent_since = t
            self.ocp_observed_dwell_s = t - self.overcurrent_since
            if self.ocp_observed_dwell_s + 1e-12 >= s.ocp_delay_s:
                self._latch("OCP_SW", t)
                return
        else:
            self.overcurrent_since = None

        if self.start_s is None:
            if measured.vin < s.uvlo_on:
                return
            self.start_s, self.start_v = t, measured.vout
        elif measured.vin < s.uvlo_off:
            self._latch("UVLO", t)
            return
        fraction = min(1.0, (t - self.start_s) / s.ramp_s) if s.ramp_s else 1.0
        v_ref = self.start_v + (s.v_set - self.start_v) * fraction
        errors = {"CV": v_ref - measured.vout, "CC": s.i_set - measured.iout}
        gains = {"CV": s.cv_pi, "CC": s.cc_pi}
        proposed_i = {mode: self.integral[mode] + gains[mode][1] * error
                      for mode, error in errors.items()}
        outputs = {mode: gains[mode][0] * error + proposed_i[mode]
                   for mode, error in errors.items()}
        self.mode = min(outputs, key=outputs.get)
        requested = outputs[self.mode]
        self.pending_ccr = self._compare_value(requested)
        for mode, error in errors.items():
            kp = gains[mode][0]
            if mode != self.mode:
                self.integral[mode] = applied_ccr / s.counts - kp * error
            elif requested < s.ccr_min / s.counts or requested > s.ccr_max / s.counts:
                self.integral[mode] = self.pending_ccr / s.counts - kp * error
            else:
                self.integral[mode] = proposed_i[mode]
