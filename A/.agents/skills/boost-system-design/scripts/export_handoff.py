"""阶段 9 — 生成交接文件（下游模块的输入）.

输出（均写入 workdir）：
  design_spec.json   系统设计基线：需求、功能块、网络、接口、器件、控制/保护参数、引脚规划、测试计划、追踪
  bom_module1.csv    主要器件清单（位号/值/封装/立创编号），供嘉立创 EDA 原理图模块使用
  boost_params.h     固件常量（PWM、采样换算、PI 参数、保护阈值），供程序模块使用
  report_tables.md   报告用数据表（自动生成，勿手改；改设计后重跑）

用法: python export_handoff.py <workdir> [--db parts_db.json]
"""

import argparse
import csv
from datetime import date
from pathlib import Path

from _common import KNOWLEDGE_DIR, load_json, param, r3, save_json, workdir_paths
from stage_params import selected

ROLE_META = {  # role → (位号前缀/位号, 功能块, 网络连接说明)
    "terminal": ("J1", "MOD-IN/MOD-OUT", "J1=输入 VIN_RAW/GND，J2=输出 VOUT/GND"),
    "fuse": ("F1", "MOD-IN", "VIN_RAW → VIN_F"),
    "pmos": ("Q2", "MOD-IN", "防反接/缓启动：D=VIN_F, S=VIN, G 经 RC 至 GND"),
    "cin_ceramic": ("C1", "MOD-IN", "VIN – GND，靠近 L1 与 Q1 源极地"),
    "cin_bulk": ("C5", "MOD-IN", "VIN – GND，靠近输入端子（抵消电源线电感）"),
    "inductor": ("L1", "MOD-PWR", "VIN → SW"),
    "mosfet": ("Q1", "MOD-PWR", "D=SW, S=PGND, G=GATE"),
    "diode": ("D1", "MOD-PWR", "A=SW, K=VBOOST"),
    "cout_ceramic": ("C10", "MOD-PWR", "VBOOST – PGND，紧靠 D1 阴极与 Q1 源极（功率回路最小）"),
    "cout_bulk": ("C20", "MOD-PWR", "VBOOST – PGND（储能/环路阻尼）"),
    "shunt": ("R1", "MOD-SENSE", "VBOOST → VOUT（高边分流）"),
    "csa": ("U3", "MOD-SENSE", "IN+=VBOOST, IN−=VOUT, OUT=IOUT_SNS, VS=+3V3"),
    "tvs": ("D2", "MOD-OUT", "VOUT – GND"),
    "gate_driver": ("U2", "MOD-DRV", "IN=PWM_H, 输出→栅极电阻→GATE, VDD=VIN（分离输出时开通/关断电阻可分设）"),
    "ldo": ("U4", "MOD-AUX", "VIN → +3V3"),
    "mcu": ("U1", "MOD-MCU", "见 pin_plan"),
    "header_swd": ("J3", "MOD-DBG", "SWD：1 3V3, 2 SWDIO, 3 SWCLK, 4 NRST, 5 GND"),
    "header_uart": ("J4", "MOD-DBG", "UART：1 3V3, 2 TX(PA9), 3 RX(PA10), 4 GND"),
}

BLOCKS = [
    ("MOD-IN", "输入接口与保护", "接线端子、保险丝、防反接/缓启动 PMOS、输入电容", "硬件", ["REQ-VIN", "REQ-PROT"]),
    ("MOD-PWR", "Boost 功率级", "电感、开关管、肖特基二极管、输出电容；把 5 V 升至 12 V", "硬件", ["REQ-VOUT", "REQ-PWR", "REQ-FSW", "REQ-RIP", "REQ-EFF"]),
    ("MOD-DRV", "栅极驱动", "把 MCU 3.3 V PWM 转为 5 V/安培级栅极驱动，缩短开关时间", "硬件", ["REQ-EFF"]),
    ("MOD-SENSE", "采样调理", "输出电压分压+RC、输出电流高边分流+电流检测放大器、输入电压分压", "硬件", ["REQ-VOUT", "REQ-IOUT", "REQ-PROT"]),
    ("MOD-MCU", "MCU 控制核心", "ADC 采样、数字 PI CC/CV、PWM、比较器硬件保护、状态机、日志", "硬件+固件", ["REQ-MCU", "REQ-CTRL", "REQ-PROT"]),
    ("MOD-AUX", "辅助电源", "5 V → 3.3 V LDO（MCU/ADC 基准/放大器供电），输入 RC 去耦", "硬件", ["REQ-MCU"]),
    ("MOD-OUT", "输出接口与保护", "输出端子、TVS、测试点", "硬件", ["REQ-PROT", "REQ-LOAD"]),
    ("MOD-DBG", "调试与人机接口", "SWD 下载口、UART 日志口、状态/故障 LED、使能按键", "硬件+固件", ["REQ-TEST"]),
]

NETS = [
    ("VIN_RAW", "输入端子正极", "power"), ("VIN_F", "保险丝后", "power"), ("VIN", "防反接后 5 V 母线", "power"),
    ("SW", "开关节点（高 dv/dt，面积最小）", "power-noisy"), ("VBOOST", "二极管阴极/输出电容", "power"),
    ("VOUT", "分流电阻后输出（接端子）", "power"), ("GATE", "Q1 栅极", "drive"), ("GATE_DRV", "驱动器输出（经栅极电阻到 GATE）", "drive"),
    ("PWM_H", "MCU TIM1_CH1 → 驱动器输入", "signal"), ("VOUT_SNS", "输出电压分压点 → ADC/COMP", "analog"),
    ("IOUT_SNS", "电流放大器输出 → ADC/COMP", "analog"), ("VIN_SNS", "输入电压分压点 → ADC", "analog"),
    ("+3V3", "LDO 输出", "power-aux"), ("GND", "统一地（功率地 PGND 与信号地在 Cout 负端单点汇合，PCB 分区）", "ground"),
    ("SWDIO", "调试", "debug"), ("SWCLK", "调试", "debug"), ("NRST", "复位", "debug"),
    ("UART_TX", "日志输出", "debug"), ("UART_RX", "命令输入", "debug"),
]


def run(req, ch, calc, sel, loss, loop, chk, db):
    s, prot = calc["sensing"], calc["protection"]
    parts = []
    for role, (des, blk, conn) in ROLE_META.items():
        p, qty = selected(sel, db, role)
        if not p:
            continue
        val = {"inductor": f"{param(p, 'inductance_uh')}µH", "cout_ceramic": f"{param(p, 'capacitance_uf')}µF/{param(p, 'voltage_v')}V",
               "cout_bulk": f"{param(p, 'capacitance_uf')}µF/{param(p, 'voltage_v')}V",
               "cin_ceramic": f"{param(p, 'capacitance_uf')}µF/{param(p, 'voltage_v')}V",
               "cin_bulk": f"{param(p, 'capacitance_uf')}µF/{param(p, 'voltage_v')}V",
               "shunt": f"{param(p, 'resistance_mohm')}mΩ 1%"}.get(role, p.get("mpn"))
        if qty > 1:
            n0 = int(des[1:])
            des = ",".join(f"{des[0]}{n0+i}" for i in range(qty))
        parts.append({"designator": des, "role": role, "block": blk, "value": val, "mpn": p.get("mpn"),
                      "manufacturer": p.get("manufacturer"), "package": p.get("package"), "qty": qty,
                      "lcsc": p.get("lcsc"), "datasheet": p.get("datasheet_url"),
                      "connection": conn + (f"；引脚：{p['pinout']}" if p.get("pinout") else "")
                      + (f"；可替代：{p['alt_mpn']}" if p.get("alt_mpn") else "")})
    vd, idv = s["vout"], s["vin"]
    passives = [
        {"designator": "R2,R3", "value": f"{vd['r_top_ohm']/1e3:g}k / {vd['r_bot_ohm']/1e3:g}k 1% 0603", "block": "MOD-SENSE",
         "connection": "VOUT–R2–VOUT_SNS–R3–GND", "note": f"分压比 {vd['ratio']}"},
        {"designator": "C30", "value": f"{vd['filter_c_f']*1e9:g}nF C0G 0603", "block": "MOD-SENSE",
         "connection": "VOUT_SNS–GND，靠近 MCU 引脚", "note": f"fc≈{vd['filter_fc_hz']:.0f} Hz"},
        {"designator": "R4,R5", "value": f"{idv['r_top_ohm']/1e3:g}k / {idv['r_bot_ohm']/1e3:g}k 1% 0603", "block": "MOD-SENSE",
         "connection": "VIN–R4–VIN_SNS–R5–GND", "note": f"分压比 {idv['ratio']}"},
        {"designator": "C31", "value": f"{idv['filter_c_f']*1e9:g}nF 0603", "block": "MOD-SENSE", "connection": "VIN_SNS–GND"},
        {"designator": "R6,C32", "value": "1k + 10nF（fc≈16 kHz）", "block": "MOD-SENSE", "connection": "IOUT_SNS RC 滤波"},
        {"designator": "R7", "value": "10Ω 0603", "block": "MOD-DRV", "connection": "GATE_DRV–GATE（抑制振铃，可调）"},
        {"designator": "R8", "value": "10k 0603", "block": "MOD-DRV", "connection": "GATE–GND 下拉（MCU 复位期间保持关断）"},
        {"designator": "R9", "value": "10k 0603", "block": "MOD-DRV", "connection": "PWM_H–GND 下拉"},
        {"designator": "C40..", "value": "100nF ×n + 1µF + 4.7µF", "block": "MOD-MCU/MOD-AUX", "connection": "每个 VDD/VDDA 引脚就近去耦"},
    ]
    mcu, _ = selected(sel, db, "mcu")
    pin_plan = (mcu or {}).get("pin_plan", {})
    full = 2 ** s["adc_bits"] - 1
    interfaces = [
        {"id": "IF-01", "signal": "PWM_H", "from": "MOD-MCU", "to": "MOD-DRV", "type": "数字 3.3 V",
         "spec": f"{calc['inputs']['fsw_hz']/1e3:g} kHz，0–{prot['duty_limit']['dmax']*100:.0f}% 占空比，高有效；复位期间下拉为低"},
        {"id": "IF-02", "signal": "GATE", "from": "MOD-DRV", "to": "MOD-PWR", "type": "栅极驱动",
         "spec": f"0/Vin 摆幅，tr≈{loss['cases']['nom']['t_rise_ns']} ns, tf≈{loss['cases']['nom']['t_fall_ns']} ns（估）"},
        {"id": "IF-03", "signal": "VOUT_SNS", "from": "MOD-SENSE", "to": "MOD-MCU (ADC + COMP)", "type": "模拟 0–3.3 V",
         "spec": f"Vout = code × {s['adc_vref_v']}/{full} / {vd['ratio']}；12 V→code {vd['code_at_vset']}；满量程 {vd['fullscale_v']} V"},
        {"id": "IF-04", "signal": "IOUT_SNS", "from": "MOD-SENSE", "to": "MOD-MCU (ADC + COMP)", "type": "模拟 0–3.3 V",
         "spec": f"Iout = code × {s['adc_vref_v']}/{full} / {s['iout']['transimpedance_v_per_a']}；0.5 A→code {s['iout']['code_at_icc']}；满量程 {s['iout']['fullscale_a']} A"},
        {"id": "IF-05", "signal": "VIN_SNS", "from": "MOD-SENSE", "to": "MOD-MCU (ADC)", "type": "模拟 0–3.3 V",
         "spec": f"Vin = code × {s['adc_vref_v']}/{full} / {idv['ratio']}；满量程 {idv['fullscale_v']} V"},
        {"id": "IF-06", "signal": "+3V3", "from": "MOD-AUX", "to": "MOD-MCU, MOD-SENSE", "type": "电源",
         "spec": f"3.3 V，负载约 {calc['aux']['i_3v3_ma']} mA；同时作为 ADC 基准（VDDA）"},
        {"id": "IF-07", "signal": "SWD/UART", "from": "MOD-DBG", "to": "PC", "type": "调试",
         "spec": f"SWD 4 线 + UART {ch['comms']['uart_baud']} bps，日志 {ch['comms']['log_rate_hz']} Hz"},
        {"id": "IF-08", "signal": "VIN_RAW/VOUT", "from": "实验电源/电子负载", "to": "MOD-IN/MOD-OUT", "type": "功率端子",
         "spec": f"输入 5 V ±5%，约 {loss['cases']['min']['iin_avg_A']} A；输出 12 V/0.5 A；CR 测 CC 段 {calc['cc_range']['r_min_ohm']}–{calc['cc_range']['r_max_ohm']} Ω"},
    ]
    cv, cc = loop["CV"], loop["CC"]
    control = {
        "structure": "CV 与 CC 两个 PI 并行计算占空比，取较小者输出（最小值选择）；非激活环积分器钳位到当前输出（防积分饱和）",
        "f_pwm_hz": calc["inputs"]["fsw_hz"], "f_ctrl_hz": calc["timing"]["f_ctrl_hz"], "tim1_rcr": calc["timing"]["tim1_rcr"],
        "pwm_arr": calc["pwm"]["arr"], "pwm_ccr_max": calc["pwm"]["ccr_max"],
        "adc_trigger": "TIM1 TRGO2 在导通区间中点触发（避开开关沿），注入组或规则组+DMA",
        "cv_pi": {k: cv.get(k) for k in ("Kp", "Ki", "Ki_Tc", "fc_min_Hz", "units")},
        "cc_pi": {k: cc.get(k) for k in ("Kp", "Ki", "Ki_Tc", "fc_min_Hz", "units")},
        "duty_limit": [prot["duty_limit"]["dmin"], prot["duty_limit"]["dmax"]],
        "soft_start_ms": prot["soft_start"]["ramp_ms"],
        "states": ["INIT", "CALIB(ADC 零点)", "STANDBY(PWM 关，等待使能且 Vin 正常)", "SOFTSTART(Vref 斜坡)",
                   "RUN(CV/CC 自动切换)", "FAULT(锁存，PWM 关；按键/串口清除且条件消失后回 STANDBY)"],
        "validation": "PI 参数为平均模型网格搜索所得初值；须经程序模块仿真/实测确认（阶跃响应、模式切换）",
    }
    tests = [
        ("T01", "REQ-VOUT", "CV 稳态精度", "Vin=4.75/5/5.25 V，电子负载 CC 0.1/0.25/0.45 A", "|Vout−12|/12 ≤ 5%"),
        ("T02", "REQ-IOUT", "CC 稳态精度", f"电子负载 CR {calc['cc_range']['r_min_ohm']}/18/22 Ω", "|Iout−0.5|/0.5 ≤ 5%"),
        ("T03", "REQ-CTRL", "CV→CC 模式切换", "CR 从 30 Ω 阶跃到 16 Ω", "无振荡，过冲 < 10%"),
        ("T04", "REQ-RIP", "输出纹波", "额定点，示波器 20 MHz 带宽限制、短地针", "Vpp/Vout ≤ 3%"),
        ("T05", "REQ-EFF", "效率", "额定点及 25/50/75% 负载，四表法测 Vin/Iin/Vout/Iout", "额定点 ≥ 75%"),
        ("T06", "REQ-PROT", "过压保护", "断开反馈/软件注入，或负载突卸", f"Vout 峰值 < {prot['ovp_hw']['value']*1.05:.1f} V，PWM 关断并锁存"),
        ("T07", "REQ-PROT", "过流保护", "CR 逐步减小至 10 Ω 以下", f"Iout > {prot['ocp_sw']['value']} A 持续 {prot['ocp_sw']['delay_ms']} ms 关断"),
        ("T08", "REQ-PROT", "占空比限幅", "Vin=4.75 V 过载，读 CCR 日志", f"CCR ≤ {prot['duty_limit']['ccr_max']}"),
        ("T09", "REQ-PROT", "软启动", "使能后录 Vout、Iin 波形", f"约 {prot['soft_start']['ramp_ms']} ms 单调上升，无过冲"),
        ("T10", "REQ-VIN", "输入波动", "Vin 在 4.75↔5.25 V 间阶跃", "Vout 恢复到 ±5% 内"),
        ("T11", "REQ-PWR", "连续运行", "额定点 30 min，测温", "无保护误动，热点 < 85 °C"),
    ]
    reqs = [
        ("REQ-VIN", "输入 5 V ±5%", req["vin"]["level"]), ("REQ-VOUT", "输出 12 V，误差 ≤ ±5%", req["vout"]["level"]),
        ("REQ-IOUT", "恒流 0.5 A，误差 ≤ ±5%", req["iout"]["level"]), ("REQ-PWR", "额定 6 W 连续运行", req["pout"]["level"]),
        ("REQ-FSW", "开关频率 50–100 kHz（采用 100 kHz）", req["fsw"]["level"]), ("REQ-RIP", "纹波 ≤5%（目标 ≤3%）", req["ripple"]["level"]),
        ("REQ-MCU", "32 位 MCU（STM32）", req["controller"]["level"]), ("REQ-CTRL", "数字 PI CC/CV", req["control"]["level"]),
        ("REQ-PROT", "过压/过流/占空比限幅/软启动", req["protection"]["level"]), ("REQ-EFF", "额定效率 ≥ 75%（实测）", req["efficiency"]["level"]),
        ("REQ-LOAD", "电子负载 CC/CR 测试", req["test_load"]["level"]), ("REQ-PCB", "两层板，功率/控制分区", req["pcb"]["level"]),
        ("REQ-TEST", "调试与测试接口", "must"),
    ]
    trace = []
    for rid, text, lvl in reqs:
        trace.append({"req": rid, "text": text, "level": lvl, "blocks": [b[0] for b in BLOCKS if rid in b[4]],
                      "tests": [t[0] for t in tests if t[1] == rid],
                      "checks": [c["id"] for c in chk["checks"] if c.get("ref") == rid]})

    spec = {
        "schema": "boost-design-spec/1", "revision": ch.get("revision"), "date": date.today().isoformat(),
        "status": chk["status"], "sdrc_summary": chk["summary"],
        "source_documents": [req.get("source_document"), "design_choices.json", "calc/selection/losses/loop/check.json"],
        "requirements": [{"id": r, "text": t, "level": l} for r, t, l in reqs],
        "topology": {"type": ch["topology"], "fsw_hz": calc["inputs"]["fsw_hz"],
                     "rationale": ch.get("rationale", {}).get("topology", "见设计报告 §3")},
        "operating_point_nominal": loss["cases"]["nom"],
        "functional_blocks": [{"id": b[0], "name": b[1], "function": b[2], "impl": b[3], "requirements": b[4]} for b in BLOCKS],
        "nets": [{"name": n, "desc": d, "class": c} for n, d, c in NETS],
        "interfaces": interfaces,
        "components": parts, "generic_passives": passives,
        "mcu_pin_plan": pin_plan,
        "sensing": s, "protection": prot, "control": control, "cc_range": calc["cc_range"],
        "pcb_guidance": [
            "功率回路 Q1(D)–D1–C10(Cout 陶瓷)–Q1(S) 面积最小，C10 紧贴 D1 阴极和 Q1 源极",
            "输入回路 C1–L1–Q1 次之；SW 铜皮够用即可，不宜过大（EMI）",
            "PGND 与信号 GND 在输出电容负端/分流点附近单点汇合；底层整面铺地",
            "分流电阻 R1 用开尔文连接走差分线到 U3；VOUT_SNS 从输出端子处取样",
            "MCU、采样 RC 远离 SW 节点与电感；驱动器 U2 紧靠 Q1，栅极回路短",
            "预留测试点：SW、GATE、VOUT、IOUT_SNS、VOUT_SNS、PWM_H、GND 钩",
            "输入/输出端子 5.08 mm；预留 SWD 与 UART 排针",
        ],
        "verification_plan": [{"id": t[0], "req": t[1], "item": t[2], "condition": t[3], "criterion": t[4], "status": "计划"} for t in tests],
        "traceability": trace,
        "open_issues": [c["id"] + " " + c["rule"] + "：" + c["detail"] for c in chk["checks"] if c["verdict"] != "PASS"],
    }
    return spec, parts, passives


def write_header(path, calc, loop, ch):
    s, prot, cv, cc = calc["sensing"], calc["protection"], loop["CV"], loop["CC"]
    full = 2 ** s["adc_bits"] - 1
    L = [
        "/* boost_params.h — 由模块一系统设计智能体自动生成（export_handoff.py），请勿手改；改设计后重新生成 */",
        f"/* revision {ch.get('revision')} */", "#ifndef BOOST_PARAMS_H", "#define BOOST_PARAMS_H", "",
        "/* ---- PWM / 时序 ---- */",
        f"#define PWM_FSW_HZ          {calc['inputs']['fsw_hz']:.0f}u",
        f"#define PWM_TIM_CLK_HZ      {calc['pwm']['timer_clk_hz']:.0f}u",
        f"#define PWM_ARR             {calc['pwm']['arr']}u      /* 边沿对齐 */",
        f"#define PWM_CCR_MAX         {prot['duty_limit']['ccr_max']}u      /* Dmax = {prot['duty_limit']['dmax']} */",
        f"#define CTRL_FREQ_HZ        {calc['timing']['f_ctrl_hz']}u",
        f"#define CTRL_TS_S           ({1/calc['timing']['f_ctrl_hz']:.6g}f)",
        f"#define TIM1_RCR            {calc['timing']['tim1_rcr']}u        /* 每 RCR+1 个 PWM 周期进一次控制中断 */",
        "", "/* ---- 采样换算：物理量 = code * K ---- */",
        f"#define ADC_FULL_SCALE      {full}u",
        f"#define ADC_VREF_V          ({s['adc_vref_v']}f)",
        f"#define VOUT_V_PER_CODE     ({s['adc_vref_v']/full/s['vout']['ratio']:.7g}f)  /* 分压 {s['vout']['r_top_ohm']:.0f}/{s['vout']['r_bot_ohm']:.0f} */",
        f"#define IOUT_A_PER_CODE     ({s['adc_vref_v']/full/s['iout']['transimpedance_v_per_a']:.7g}f)  /* Rs={s['iout']['shunt_ohm']}Ω × G{s['iout']['gain']} */",
        f"#define VIN_V_PER_CODE      ({s['adc_vref_v']/full/s['vin']['ratio']:.7g}f)",
        "", "/* ---- 设定值 ---- */",
        f"#define VOUT_SET_V          ({calc['inputs']['vout_v']}f)",
        f"#define IOUT_SET_A          ({calc['inputs']['iout_cc_a']}f)",
        f"#define SOFTSTART_MS        {prot['soft_start']['ramp_ms']}u",
        "", "/* ---- PI（u=占空比 0..1；e 为物理量误差）初值，需联调确认 ---- */",
        f"#define CV_KP               ({cv.get('Kp')}f)   /* 1/V */",
        f"#define CV_KI_TS            ({cv.get('Ki_Tc')}f)   /* Ki*Ts, 1/V per sample */",
        f"#define CC_KP               ({cc.get('Kp')}f)   /* 1/A */",
        f"#define CC_KI_TS            ({cc.get('Ki_Tc')}f)   /* Ki*Ts, 1/A per sample */",
        f"#define DUTY_MIN            ({prot['duty_limit']['dmin']}f)",
        f"#define DUTY_MAX            ({prot['duty_limit']['dmax']}f)",
        "", "/* ---- 保护 ---- */",
        f"#define OVP_SW_V            ({prot['ovp_sw']['value']:.3g}f)",
        f"#define OVP_HW_CODE         {prot['ovp_hw']['adc_code']}u    /* COMP 参考 DAC 码值，对应 {prot['ovp_hw']['value']:.3g} V */",
        f"#define OCP_SW_A            ({prot['ocp_sw']['value']:.3g}f)",
        f"#define OCP_SW_DELAY_MS     {prot['ocp_sw']['delay_ms']}u",
        f"#define OCP_HW_CODE         {prot['ocp_hw']['adc_code']}u    /* 对应 {prot['ocp_hw']['value']} A */",
        f"#define UVLO_OFF_V          ({prot['uvlo']['off_v']}f)",
        f"#define UVLO_ON_V           ({prot['uvlo']['on_v']}f)",
        "", "#endif", "",
    ]
    Path(path).write_text("\n".join(L), encoding="utf-8")


def md_table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join("" if c is None else str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def write_tables(path, spec, calc, sel, loss, loop, chk):
    T = [f"# 模块一 自动生成数据表（revision {spec['revision']}，{spec['date']}）", "",
         "> 由 export_handoff.py 生成，勿手改。报告正文引用本文件中的表格。", ""]
    T += ["## 表 A 需求追踪矩阵", md_table(["需求", "内容", "约束", "功能块", "检查项", "测试"],
          [(t["req"], t["text"], t["level"], ", ".join(t["blocks"]), ", ".join(t["checks"][:4]) + ("…" if len(t["checks"]) > 4 else ""),
            ", ".join(t["tests"])) for t in spec["traceability"]]), ""]
    T += ["## 表 B 关键参数计算（初算）", md_table(["编号", "参数", "公式", "代入", "结果", "单位", "说明"],
          [(x["id"], x["name"], x["formula"], x["substitution"], x["value"], x["unit"], x["note"]) for x in calc["steps"]]), ""]
    T += ["## 表 C 器件复核计算", md_table(["编号", "参数", "公式", "代入", "结果", "单位", "说明"],
          [(x["id"], x["name"], x["formula"], x["substitution"], x["value"], x["unit"], x["note"]) for x in loss["steps"]]), ""]
    T += ["## 表 D 损耗分解（Vin 标称/最小）", md_table(["损耗项", "Vin_nom / W", "Vin_min / W"],
          [(k, v, loss["cases"]["min"]["losses_W"][k]) for k, v in loss["cases"]["nom"]["losses_W"].items()]
          + [("**合计**", loss["cases"]["nom"]["p_total_W"], loss["cases"]["min"]["p_total_W"]),
             ("**效率 η**", f"{loss['cases']['nom']['eta_total']*100:.1f}%", f"{loss['cases']['min']['eta_total']*100:.1f}%")]), ""]
    rows = []
    for cat, v in sel["categories"].items():
        for c in v["candidates"]:
            fails = [r["rule"] for r in c["rules"] if not r["ok"] and r["hard"]]
            rows.append((cat, c["mpn"], c["package"], c["lcsc"] or "待核实", "✔ 入选" if c["id"] == v["selected"] else ("通过" if c["pass"] else "淘汰"),
                         c["score"], "；".join(fails) if fails else c["score_note"]))
    T += ["## 表 E 器件候选与选型结果", md_table(["类别", "型号", "封装", "立创编号", "结论", "评分", "淘汰原因/评分依据"], rows), ""]
    for m in ("CV", "CC"):
        r = loop[m]
        T += [f"## 表 F-{m} {m} 环路各工况角（Kp={r.get('Kp')}, Ki={r.get('Ki')}）",
              md_table(["Vin", "负载", "D", "f0/Hz", "fRHPZ/Hz", "fc/Hz", "PM/°", "GM/dB"],
                       [(c["vin_key"], c["load"], c.get("D"), c.get("f0_Hz"), c.get("fRHPZ_Hz"), c.get("fc_Hz"), c.get("PM_deg"), c.get("GM_dB"))
                        for c in r["corners"]]), ""]
    T += ["## 表 G 系统设计规则检查（SDRC）", f"汇总：{chk['summary']} → **{chk['status']}**", "",
          md_table(["编号", "规则", "结论", "依据"], [(c["id"], c["rule"], c["verdict"], c["detail"]) for c in chk["checks"]]), ""]
    T += ["## 表 H 测试计划", md_table(["编号", "需求", "项目", "条件", "判据", "状态"],
          [(t["id"], t["req"], t["item"], t["condition"], t["criterion"], t["status"]) for t in spec["verification_plan"]]), ""]
    Path(path).write_text("\n".join(T), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir")
    ap.add_argument("--db", default=str(KNOWLEDGE_DIR / "parts_db.json"))
    a = ap.parse_args()
    p = workdir_paths(a.workdir)
    J = {k: load_json(p[k]) for k in ("requirements", "choices", "calc", "selection", "losses", "loop", "check")}
    db = load_json(a.db)
    spec, parts, passives = run(J["requirements"], J["choices"], J["calc"], J["selection"], J["losses"], J["loop"], J["check"], db)
    w = Path(a.workdir)
    save_json(w / "design_spec.json", spec)
    with open(w / "bom_module1.csv", "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["位号", "名称/值", "封装", "数量", "制造商料号", "制造商", "立创编号", "功能块", "连接说明"])
        for x in parts:
            wr.writerow([x["designator"], x["value"], x["package"], x["qty"], x["mpn"], x["manufacturer"], x["lcsc"] or "待核实",
                         x["block"], x["connection"]])
        for x in passives:
            wr.writerow([x["designator"], x["value"], "", "", "", "", "嘉立创基础库按值选取", x["block"], x["connection"]])
    write_header(w / "boost_params.h", J["calc"], J["loop"], J["choices"])
    write_tables(w / "report_tables.md", spec, J["calc"], J["selection"], J["losses"], J["loop"], J["check"])
    print(f"design_spec.json / bom_module1.csv / boost_params.h / report_tables.md written; status={spec['status']}, "
          f"open issues={len(spec['open_issues'])}")


if __name__ == "__main__":
    main()
