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
from handoff_header import write_header
from handoff_contract import issues, proposed_pin_plan, requirement_rows, traceability, verification_plan
from trace_graph import extend_traceability

ROLE_META = {  # role → (位号前缀/位号, 功能块, 网络连接说明)
    "terminal": ("J1", "MOD-IN/MOD-OUT", "J1=输入 VIN_RAW/GND，J2=输出 VOUT/GND"),
    "fuse": ("F1", "MOD-IN", "VIN_RAW → VIN_F"),
    "pmos": ("Q2", "MOD-IN", "防反接：D=VIN_F, S=VIN；栅极网络由 B 核实；未实现经验证的浪涌限制"),
    "cin_ceramic": ("C1", "MOD-IN", "VIN – GND，靠近 L1 与 Q1 源极地"),
    "cin_bulk": ("C5", "MOD-IN", "VIN – GND，靠近输入端子（抵消电源线电感）"),
    "inductor": ("L1", "MOD-PWR", "VIN → SW"),
    "mosfet": ("Q1", "MOD-PWR", "D=SW, S=GND（功率回流区）, G=GATE"),
    "diode": ("D1", "MOD-PWR", "A=SW, K=VBOOST"),
    "cout_ceramic": ("C10", "MOD-PWR", "VBOOST – GND（功率回流区），紧靠 D1 阴极与 Q1 源极（功率回路最小）"),
    "cout_bulk": ("C20", "MOD-PWR", "VBOOST – GND（功率回流区；储能/环路阻尼）"),
    "shunt": ("R1", "MOD-SENSE", "VBOOST → VOUT（高边分流）"),
    "csa": ("U3", "MOD-SENSE", "IN+=VBOOST, IN−=VOUT, OUT=IOUT_SENSE_RAW, VS=+3V3；经 R6/C32 到 IOUT_SENSE；COMP 支路和延迟待核实"),
    "tvs": ("D2", "MOD-OUT", "VOUT – GND"),
    "gate_driver": ("U2", "MOD-DRV", "IN=PWM_SW, 输出=GATE_DRV→栅极电阻→GATE, VDD=VIN（分离输出时开通/关断电阻可分设）"),
    "ldo": ("U4", "MOD-AUX", "VIN → +3V3"),
    "mcu": ("U1", "MOD-MCU", "见 pin_plan"),
    "header_swd": ("J3", "MOD-DBG", "SWD：1 3V3, 2 SWDIO, 3 SWCLK, 4 NRST, 5 GND"),
    "header_uart": ("J4", "MOD-DBG", "UART：1 3V3, 2 TX(PA9), 3 RX(PA10), 4 GND"),
}

BLOCKS = [
    ("MOD-IN", "输入接口与保护", "端子、保险丝、防反接 PMOS、输入电容；独立输入限流待关闭", "硬件", ["REQ-IN", "REQ-PWR", "REQ-PROT"]),
    ("MOD-PWR", "Boost 功率级", "电感、开关管、二极管、输出电容", "硬件", ["REQ-CV", "REQ-CC", "REQ-PWR", "REQ-FSW", "TGT-FSW", "REQ-RIPPLE", "TGT-RIPPLE", "TGT-ETA"]),
    ("MOD-DRV", "栅极驱动", "MCU PWM 经独立 B 版驱动器驱动 MOSFET", "硬件", ["REQ-FSW", "REQ-PROT", "TGT-ETA"]),
    ("MOD-SENSE", "采样调理", "端子电压、输出电容后高边负载电流、输入电压；校准与滤波", "硬件", ["REQ-IN", "REQ-CV", "REQ-CC", "REQ-PROT", "REQ-ETA-MEAS"]),
    ("MOD-MCU", "MCU 控制核心", "ADC、数字 PI CC/CV、PWM、比较器刹车、状态机", "硬件+固件", ["REQ-MCU", "TGT-MCU", "REQ-CTRL", "REQ-PROT", "TGT-START", "REQ-FSW", "TGT-FSW"]),
    ("MOD-AUX", "辅助电源", "LDO 与去耦，模拟参考环境", "硬件", ["REQ-IN", "REQ-MCU", "REQ-CV", "REQ-CC"]),
    ("MOD-OUT", "输出接口与保护", "输出端子、TVS、测试点；不具备输出隔离", "硬件", ["REQ-CV", "REQ-CC", "REQ-PROT", "REQ-LOAD"]),
    ("MOD-DBG", "调试接口", "SWD、UART、状态指示、外部仪表测试", "硬件+固件", ["REQ-LOAD", "REQ-ETA-MEAS", "TGT-ETA"]),
    ("MOD-ENG", "跨模块工程约束", "B/C/D 工程、采购和装配审查", "工程验证", ["REQ-PCB", "REQ-BUILD", "REQ-EDA", "REQ-SOURCE"]),
]

NETS = [
    ("VIN_RAW", "输入端子正极", "power"), ("VIN_F", "保险丝后", "power"), ("VIN", "防反接后 5 V 母线", "power"),
    ("SW", "开关节点（高 dv/dt，面积最小）", "power-noisy"), ("VBOOST", "二极管阴极/输出电容", "power"),
    ("VOUT", "分流电阻后输出（接端子）", "power"), ("GATE", "Q1 栅极", "drive"), ("GATE_DRV", "驱动器输出（经栅极电阻到 GATE）", "drive"),
    ("PWM_SW", "MCU TIM1_CH1 → 驱动器输入", "signal"), ("VOUT_SENSE", "输出电压分压点 → ADC/COMP", "analog"),
    ("IOUT_SENSE_RAW", "电流放大器输出，R6/C32 之前；COMP 取样支路待 SR46 核实", "analog"),
    ("IOUT_SENSE", "R6/C32 之后的电流 ADC 信号；硬件比较器路径和延迟待核实", "analog"), ("VIN_SENSE", "输入电压分压点 → ADC", "analog"),
    ("+3V3", "LDO 输出", "power-aux"), ("GND", "唯一地网络（不另设 PGND/AGND 网络）：连续参考地平面；功率回流分区，不让开关回流穿过采样地；“功率回流区”仅是布局分区说明，不在高边分流处接地", "ground"),
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
                      "part_id": p['id'], "confidence": p.get('confidence'), "parameter_evidence": p.get('params'),
                      "lcsc_evidence": p.get('lcsc_evidence'), "stock_risk": p.get('stock_risk', False),
                      "connection": conn + (f"；引脚：{p['pinout']}" if p.get("pinout") else "")
                      + (f"；库内替代提案（采用前重审）：{p['alt_mpn']}" if p.get("alt_mpn") else "")})
    vd, idv = s["vout"], s["vin"]
    passives = [
        {"designator": "R2,R3", "value": f"{vd['r_top_ohm']/1e3:g}k / {vd['r_bot_ohm']/1e3:g}k 1% 0603", "block": "MOD-SENSE",
         "connection": "VOUT–R2–VOUT_SENSE–R3–GND", "note": f"分压比 {vd['ratio']}"},
        {"designator": "C30", "value": f"{vd['filter_c_f']*1e9:g}nF C0G 0603", "block": "MOD-SENSE",
         "connection": "VOUT_SENSE–GND，靠近 MCU 引脚", "note": f"fc≈{vd['filter_fc_hz']:.0f} Hz"},
        {"designator": "R4,R5", "value": f"{idv['r_top_ohm']/1e3:g}k / {idv['r_bot_ohm']/1e3:g}k 1% 0603", "block": "MOD-SENSE",
         "connection": "VIN–R4–VIN_SENSE–R5–GND", "note": f"分压比 {idv['ratio']}"},
        {"designator": "C31", "value": f"{idv['filter_c_f']*1e9:g}nF 0603", "block": "MOD-SENSE", "connection": "VIN_SENSE–GND"},
        {"designator": "R6", "value": f"{ch['losses']['current_filter_r_ohm']:g}Ω", "block": "MOD-SENSE", "connection": "IOUT_SENSE_RAW–R6–IOUT_SENSE"},
        {"designator": "C32", "value": f"{ch['losses']['current_filter_c_f']*1e9:g}nF", "block": "MOD-SENSE", "connection": "IOUT_SENSE–GND；与 S7 模型一致"},
        {"designator": "R7", "value": f"{ch['losses']['gate_resistor_ohm']:g}Ω", "block": "MOD-DRV", "connection": "GATE_DRV–GATE；更改后复核开关损耗与振铃"},
        {"designator": "R8", "value": "下拉阻值/额定待 B 设计与查证", "block": "MOD-DRV", "connection": "GATE–GND 下拉（MCU 复位期间保持关断）"},
        {"designator": "R9", "value": "下拉阻值/额定待 B 设计与查证", "block": "MOD-DRV", "connection": "PWM_SW–GND 下拉"},
        {"designator": "待 B 分配", "value": "去耦/复位/BOOT/LED/按键/PMOS栅极等未齐套", "block": "MOD-MCU/MOD-AUX/MOD-IN/MOD-DBG", "connection": "按有出处的器件手册补齐；不是已选物料"},
    ]
    for x in passives:
        x.update(status='engineering_value_proposal_not_selected', lcsc=None,
                 sourcing='B 从有出处的器件库补选；缺失时先按器件库扩充流程查证',
                 note=x.get('note', '') + '；数值/封装提案，不是已查证可采购器件')
    mcu, _ = selected(sel, db, "mcu")
    pin_plan = proposed_pin_plan(mcu or {})
    full = 2 ** s["adc_bits"] - 1
    interfaces = [
        {"id": "IF-01", "signal": "PWM_SW", "from": "MOD-MCU", "to": "MOD-DRV", "type": "数字 3.3 V",
         "spec": f"{calc['inputs']['fsw_hz']/1e3:g} kHz，0–{prot['duty_limit']['dmax']*100:.0f}% 占空比，高有效；复位期间下拉为低"},
        {"id": "IF-02", "signal": "GATE", "from": "MOD-DRV", "to": "MOD-PWR", "type": "栅极驱动",
         "spec": f"0/Vin 摆幅，tr≈{loss['cases']['nom']['t_rise_ns']} ns, tf≈{loss['cases']['nom']['t_fall_ns']} ns（模型估计）；分离输出按已建模等效栅极电阻连接，修改后重算"},
        {"id": "IF-03", "signal": "VOUT_SENSE", "from": "MOD-SENSE", "to": "MOD-MCU (ADC + COMP)", "type": "模拟 0–3.3 V",
         "spec": f"Vout = code × {s['adc_vref_v']}/{full} / {vd['ratio']}；12 V→code {vd['code_at_vset']}；满量程 {vd['fullscale_v']} V"},
        {"id": "IF-04", "signal": "IOUT_SENSE", "from": "MOD-SENSE", "to": "MOD-MCU (ADC；COMP 分支待核实)", "type": "模拟 0–3.3 V",
         "spec": f"Iout = code × {s['adc_vref_v']}/{full} / {s['iout']['transimpedance_v_per_a']}；0.5 A→code {s['iout']['code_at_icc']}；满量程 {s['iout']['fullscale_a']} A"},
        {"id": "IF-05", "signal": "VIN_SENSE", "from": "MOD-SENSE", "to": "MOD-MCU (ADC)", "type": "模拟 0–3.3 V",
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
        "structure": "双 PI 最小值选择后统一限幅；未主导环积分状态 I=d_actual−Kp*e，跟踪实际占空比；主导环条件积分或回算抗饱和，切换无扰初始化",
        "f_pwm_hz": calc["inputs"]["fsw_hz"], "f_ctrl_hz": calc["timing"]["f_ctrl_hz"], "tim1_rcr": calc["timing"]["tim1_rcr"],
        "pwm_arr": calc["pwm"]["arr"], "pwm_ccr_max": calc["pwm"]["ccr_max"],
        "adc_trigger": "；".join(ch.get("rationale", {}).get(k) for k in ("control_timing", "adc_edge_cases")
                                if ch.get("rationale", {}).get(k))
                       or "【S4 未填写 rationale.control_timing / adc_edge_cases】默认：TIM1 在导通区间中点触发 ADC，避开开关沿",
        "cv_pi": {k: cv.get(k) for k in ("Kp", "Ki", "Ki_Tc", "fc_min_Hz", "units")},
        "cc_pi": {k: cc.get(k) for k in ("Kp", "Ki", "Ki_Tc", "fc_min_Hz", "units")},
        "duty_limit": [prot["duty_limit"]["dmin"], prot["duty_limit"]["dmax"]],
        "soft_start_ms": prot["soft_start"]["ramp_ms"],
        "states": ["INIT", "CALIB(采样自检/加载标定；仅确认无实际电流才更新电流零点，不在带电输出盲目归零)", "STANDBY(PWM 关，等待显式使能且 Vin 正常)", "SOFTSTART(从实测 Vout 建立参考斜坡)",
                   "RUN(CV/CC 自动切换)", "FAULT(锁存，PWM 关；按键/串口清除且条件消失后回 STANDBY)"],
        "validation": "PI 参数为平均模型网格搜索所得初值；须经程序模块仿真/实测确认（阶跃响应、模式切换）",
        "current_filter": {'r_ohm': ch['losses']['current_filter_r_ohm'], 'c_f': ch['losses']['current_filter_c_f'],
                           'amplifier_bandwidth_source': '所选 CSA 的 parameter_evidence.bandwidth_khz，S7 使用典型值极点'},
        "model_assumptions": loop.get('assumptions', loss['assumptions']),
        "hardware_resource_status": 'SR46 open；独立阈值源/ADC/COMP/刹车路由待验证；禁止把 ADC 等效码当成 DAC 配置',
    }
    reqs = requirement_rows(req)
    tests = verification_plan(req, calc)
    blocks = [{"id": b[0], "name": b[1], "function": b[2], "impl": b[3], "requirements": b[4]} for b in BLOCKS]
    trace = traceability(reqs, blocks, tests, chk)
    extend_traceability(trace, calc, loss, loop, sel, chk)

    spec = {
        "schema": "boost-design-spec/1", "revision": ch.get("revision"), "date": date.today().isoformat(),
        "status": chk["status"], "sdrc_summary": chk["summary"],
        "source_documents": [req.get("source_document"), "requirements.json", "design_choices.json", "calc.json", "selection.json", "losses.json", "loop.json", "loop_validation.json", "check.json", "tooling/knowledge/parts_db.json"],
        "requirements": reqs,
        "topology": {"type": ch["topology"], "fsw_hz": calc["inputs"]["fsw_hz"],
                     "rationale": ch.get("rationale", {}).get("topology", "见设计报告 §3")},
        "operating_point_nominal": loss["cases"]["nom"],
        "functional_blocks": blocks,
        "nets": [{"name": n, "desc": d, "class": c} for n, d, c in NETS],
        "interfaces": interfaces,
        "components": parts, "generic_passives": passives,
        "mcu_pin_plan": pin_plan,
        "sensing": s, "protection": prot, "control": control, "cc_range": calc["cc_range"],
        "pcb_guidance": [
            "功率回路 Q1(D)–D1–C10(Cout 陶瓷)–Q1(S) 面积最小，C10 紧贴 D1 阴极和 Q1 源极",
            "输入回路 C1–L1–Q1 次之；SW 铜皮够用即可，不宜过大（EMI）",
            "连续参考地平面与功能分区；功率回流直接回到功率器件，避免穿过采样地；R1 是高边分流，不是地汇合点",
            "分流电阻 R1 用开尔文连接走差分线到 U3；VOUT_SENSE 从输出端子处取样",
            "MCU、采样 RC 远离 SW 节点与电感；驱动器 U2 紧靠 Q1，栅极回路短",
            "预留测试点：SW、GATE、VOUT、IOUT_SENSE_RAW、IOUT_SENSE、VOUT_SENSE、PWM_SW、GND 钩",
            "输入/输出端子封装按已选器件库与数据手册核对（不可用其他节距封装代换）；预留 SWD 与 UART",
        ],
        "verification_plan": tests,
        "traceability": trace,
        "open_issues": [c["id"] + " 检查目标「" + c["rule"] + "」尚未关闭；观测：" + c["detail"] for c in chk["checks"] if c["verdict"] != "PASS"],
        "implementation_closures": issues(chk),
        "handoff_scope": f"{ch.get('revision')} 系统设计交接；conditional_review，实施关闭条件保留；不是无条件制造/上电批准",
        "net_naming": "网络名遵循模块 B 规范 §12.3（VIN/VOUT/GND/PWM_SW/VOUT_SENSE/IOUT_SENSE…）；只有一个地网络 GND",
        "bom_scope": "主要器件清单；generic_passives 为未选型提案，不列入可采购 BOM；完整装配 BOM 由 B 按有证据器件库补齐",
        "data_classification": {"calculation": ["calc.json", "losses.json", "loop.json", "loop_validation.json"],
                                "switching_simulation": "未进行", "measurement": "未进行，verification_plan 均为计划"},
        "cc_boundary": "低于 CC 电压或 CR 阻值下限为不可控区，不保证恒流；上端为 CC/CV 交界；PWM 关闭不等于输出隔离",
        "acceptance_load_modes": {"CV": "CC", "CC": "CR"},
    }
    return spec, parts, passives


def md_table(head, rows):
    def cell(value):
        return '' if value is None else str(value).replace('|', '\\|').replace('\n', '<br>')
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def write_tables(path, spec, calc, sel, loss, loop, chk):
    T = [f"# 模块一 自动生成数据表（revision {spec['revision']}，{spec['date']}）", "",
         "> 由 export_handoff.py 生成，勿手改。报告正文引用本文件中的表格。", ""]
    T += ["## 表 A 需求追踪矩阵", md_table(["需求", "内容", "约束", "功能块", "计算步", "器件 ID", "检查项", "测试"],
          [(t["req"], t["text"], t["level"], ", ".join(t["blocks"]), ", ".join(t["calculation_steps"]),
            ", ".join(t["parts"]), ", ".join(t["checks"]), ", ".join(t["tests"])) for t in spec["traceability"]]), ""]
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
              md_table(["编号", "参数", "结果", "单位"], [(x["id"], x["name"], x["value"], x["unit"])
                       for x in loop.get("steps", []) if x["id"].startswith(m + "_")]),
              md_table(["模型", "Vin/V", "负载", "Vout/V", "D", "f0/Hz", "fRHPZ/Hz", "fc/Hz", "PM/°", "GM/dB"],
                       [(c.get('variant'), c.get('vin'), c["load"], c.get('vo'), c.get("D"), c.get("f0_Hz"), c.get("fRHPZ_Hz"), c.get("fc_Hz"), c.get("PM_deg"), c.get("GM_dB"))
                        for c in r["corners"]]), ""]
    T += ["## 表 G 系统设计规则检查（SDRC）", f"汇总：{chk['summary']} → **{chk['status']}**", "",
          md_table(["编号", "规则", "结论", "依据"], [(c["id"], c["rule"], c["verdict"], c["detail"]) for c in chk["checks"]]), ""]
    T += ["## 表 H 测试计划", md_table(["编号", "需求", "项目", "条件", "判据", "前置条件", "状态"],
          [(t["id"], ', '.join(t['requirements']), t["item"], t["condition"], t["criterion"], t['prerequisite'], t["status"]) for t in spec["verification_plan"]]), ""]
    T += ["## 表 I 主要器件（不是完整装配 BOM）", md_table(['位号','型号','数量','封装','立创编号','证据等级'],
          [(p['designator'],p['mpn'],p['qty'],p['package'],p['lcsc'],p['confidence']) for p in spec['components']]), '']
    T += ["## 表 J 未关闭工程项", md_table(['ID','责任','关闭节点','关闭要求'],
          [(x['id'],x['owner'],x['required_before'],x['closure']) for x in spec['implementation_closures']]), '']
    Path(path).write_text("\n".join(T), encoding="utf-8")


def handoff_inputs(workdir):
    p = workdir_paths(workdir)
    keys = ("requirements", "choices", "calc", "selection", "losses", "loop", "check")
    J = {key: load_json(p[key]) for key in keys}
    J["sim"] = load_json(p["sim"]) if p["sim"].exists() else None
    return J


def prepare_handoff(J, db):
    revisions = {J[k]['revision'] for k in ('choices', 'calc', 'selection', 'losses', 'loop', 'check')}
    if len(revisions) != 1 or J['check']['summary']['FAIL'] != 0:
        raise ValueError('交接禁止：版本不一致或 SDRC 有 FAIL')
    if not all(J['loop'][mode]['ok'] for mode in ('CV', 'CC')):
        raise ValueError('交接禁止：环路初值复核未通过')
    spec, _, _ = run(J["requirements"], J["choices"], J["calc"], J["selection"], J["losses"], J["loop"], J["check"], db)
    simulation = J["sim"]
    if simulation is not None:
        if simulation.get("revision") != spec["revision"]:
            raise ValueError("交接禁止：仿真报告版本不一致")
        spec["source_documents"].append("sim.json")
        spec["data_classification"]["switching_simulation"] = "已运行；sim.json 为仿真证据"
        spec["switching_simulation_summary"] = {
            "file": "sim.json",
            "cases": len(simulation["cases"]),
            "passed": sum(c["verdict"] == "PASS" for c in simulation["cases"]),
            "hardware_validation_required": True,
        }
    if not all(x['blocks'] and x['tests'] for x in spec['traceability']):
        raise ValueError('需求缺少功能块或验证计划')
    return spec


def write_bom(path, spec):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["位号", "名称/值", "封装", "数量", "制造商料号", "制造商", "立创编号", "功能块", "连接说明", "设计版本", "清单范围", "状态"])
        for x in spec["components"]:
            wr.writerow([x["designator"], x["value"], x["package"], x["qty"], x["mpn"], x["manufacturer"], x["lcsc"] or "待核实",
                         x["block"], x["connection"], spec['revision'], '主要器件，配套物料待补齐', spec['status']])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir", type=Path)
    ap.add_argument("--db", default=str(KNOWLEDGE_DIR / "parts_db.json"))
    a = ap.parse_args()
    w = a.workdir
    J = handoff_inputs(w)
    spec = prepare_handoff(J, load_json(a.db))
    save_json(w / "design_spec.json", spec)
    write_bom(w / "bom_module1.csv", spec)
    write_header(w / "boost_params.h", J["calc"], J["loop"], J["choices"])
    write_tables(w / "report_tables.md", spec, J["calc"], J["selection"], J["losses"], J["loop"], J["check"])
    print(f"design_spec.json / bom_module1.csv / boost_params.h / report_tables.md written; status={spec['status']}, "
          f"open issues={len(spec['open_issues'])}")


if __name__ == "__main__":
    main()
