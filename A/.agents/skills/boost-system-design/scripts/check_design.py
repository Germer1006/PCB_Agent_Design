"""阶段 8 — 系统设计规则检查（System Design Rule Check, SDRC）.

类比 PCB 的 DRC：把需求、计算、选型、复核结果放在一起做一致性与裕量检查，
每条规则给出 PASS / WARN / FAIL / NOT_ASSESSED / N/A 及数值依据。

用法: python check_design.py <workdir> [--db parts_db.json]
"""

import argparse
import fnmatch
import json
import sys

from _common import KNOWLEDGE_DIR, Checks, load_json, param, save_json, workdir_paths
from kicad_lib import INDEX as KICAD_INDEX, verify_mcu_pins, verify_part

RULES_PATH = KNOWLEDGE_DIR / "sdrc_rules.json"


def selected(selection, db, cat):
    """A missing database entry is evidence missing, not a crashed audit."""
    category = selection["categories"].get(cat, {})
    part = next((p for p in db["parts"] if p["id"] == category.get("selected")), None)
    return part, category.get("selected_qty") or 1


def _validate_rules(rules):
    required = {"id", "layer", "severity", "title", "rationale", "source",
                "verify", "applies_when", "closure_if_fail"}
    ids = set()
    for rule in rules:
        if not required.issubset(rule) or any(not isinstance(rule[k], str) or not rule[k].strip() for k in required):
            raise ValueError(f"Incomplete SDRC metadata: {rule.get('id')}")
        if rule["id"] in ids:
            raise ValueError(f"Duplicate SDRC metadata: {rule['id']}")
        ids.add(rule["id"])
        if rule["layer"] not in {"requirement", "rating", "topology", "control", "thermal", "sensing",
                                 "protection", "sourcing", "firmware", "model"}:
            raise ValueError(f"Invalid SDRC layer: {rule['id']}")
        if rule["severity"] not in {"critical", "major", "minor", "info"} or rule["verify"] not in {"automatic", "simulation", "measurement"}:
            raise ValueError(f"Invalid SDRC severity/verify: {rule['id']}")


def _rule_meta(rule_id, rules):
    candidates = [item for item in rules if fnmatch.fnmatchcase(rule_id, item["id"])]
    if len(candidates) != 1:
        raise ValueError(f"SDRC rule {rule_id} has {len(candidates)} metadata entries")
    return candidates[0]


def _decorate_checks(checks, rules):
    _validate_rules(rules)
    for item in checks:
        meta = _rule_meta(item["id"], rules)
        item["title"] = meta["title"]
        item.update({key: meta[key] for key in ("layer", "severity", "rationale", "source",
                                                 "verify", "applies_when", "closure_if_fail")})
    return checks


def _exit_code(res):
    if res["summary"]["FAIL"]:
        return 2
    if any(c["verdict"] == "NOT_ASSESSED" and c["severity"] in ("critical", "major")
           for c in res["checks"]):
        return 3
    return 0


def run(req, ch, calc, sel, loss, loop, db, sim=None):
    K = Checks()
    if sim is not None and (sim.get("schema") != "boost-switching-sim/1" or sim.get("revision") != ch.get("revision")):
        K.add("SR79", "T1 仿真报告对应本次设计版本", False, "sim.json 版本或格式不一致；重新运行 T1", ref="SIM")
        sim = None
    sim_cases = sim["cases"] if sim else []
    vo, io = calc["inputs"]["vout_v"], calc["inputs"]["iout_cc_a"]
    vin = calc["inputs"]["vin"]
    prot = calc["protection"]

    # --- 需求一致性 ---
    K.add("SR01", "额定功率与 Vo·Io 一致", abs(vo * io - req["pout"]["rated_w"]) / req["pout"]["rated_w"] <= 0.05,
          f"{vo}×{io} = {vo*io} W vs {req['pout']['rated_w']} W", ref="REQ-PWR")
    K.add("SR02", "Vin,max < Vo（Boost 适用）", vin["max"] < vo, f"{vin['max']:.2f} V < {vo} V", ref="DEC-TOPO")
    lo, hi = req["fsw"]["range_hz"]
    K.add("SR03", "开关频率在课程范围内", lo <= calc["inputs"]["fsw_hz"] <= hi, f"{calc['inputs']['fsw_hz']} Hz ∈ [{lo},{hi}]",
          ref="REQ-FSW")
    for r in req["protection"]["required"] + req["protection"]["suggested"]:
        key = {"OVP": "ovp_sw", "OCP": "ocp_sw", "duty_limit": "duty_limit", "soft_start": "soft_start"}[r]
        K.add(f"SR04-{r}", f"保护功能 {r} 已定义阈值与动作", key in prot, prot.get(key, {}).get("action", "缺失"), ref="REQ-PROT")
    K.add("SR05", "MCU 为 32 位 STM32", sel["categories"]["mcu"]["selected"] is not None and
          "STM32" in (selected(sel, db, "mcu")[0] or {}).get("mpn", ""), str(sel["categories"]["mcu"]["selected"]), ref="REQ-MCU")

    # --- 选型完整性 ---
    for cat, v in sel["categories"].items():
        K.add(f"SR10-{cat}", f"{cat} 有通过硬性规则的入选器件", v["selected"] is not None, v["reason"], ref="SEL")
    for cat, v in sel["categories"].items():
        part, _ = selected(sel, db, cat)
        K.add(f"SR11-{cat}", f"{cat} 立创商城编号已核实", None if part is None else bool(part.get("lcsc")),
              f"{part.get('mpn')} → {part.get('lcsc')}" if part else "未选器件或数据库中没有对应条目",
              warn_only=True, ref="SOURCING")

    # Validate selected mappings against the pinned official-library snapshot.
    kidx = load_json(KICAD_INDEX) if KICAD_INDEX.exists() else None
    for cat in sel["categories"]:
        part, _ = selected(sel, db, cat)
        if part:
            verdict, detail = verify_part(part, kidx) if kidx else ("FAIL", "KiCad library snapshot is missing")
            K.add(f"SR12-{cat}", f"{cat} KiCad symbol/footprint/pins are valid", verdict == "PASS", detail,
                  warn_only=(verdict == "WARN"), ref="REQ-EDA")
    mcu, _ = selected(sel, db, "mcu")
    if mcu and kidx:
        ok, detail = verify_mcu_pins(mcu, kidx)
        K.add("SR13", "MCU pin plan agrees with KiCad symbol pins", ok, detail, ref="REQ-EDA")

    # --- 占空比 / 增益峰 ---
    gp = loss["gain_peak"]
    env = loss['envelope']
    K.add("SR20", "Dmax > 已建模额定容差场景最大占空比", prot["duty_limit"]["dmax"] > env['D_max'],
          f"Dmax={prot['duty_limit']['dmax']} vs D,max={env['D_max']}", ref="CTRL-DUTY")
    K.add("SR21", "Dmax < 升压比峰值点占空比", prot["duty_limit"]["dmax"] < gp["d_peak"],
          f"Dmax={prot['duty_limit']['dmax']} < D_peak={gp['d_peak']}（峰值 Vo={gp['vo_peak_v']} V）", ref="CTRL-DUTY")
    vpk_dmax_ok = gp["vo_peak_v"] > prot["ovp_hw"]["value"]
    K.add("SR22", "失控时可达电压 > 硬件 OVP（OVP 有意义且必须存在）", vpk_dmax_ok,
          f"峰值 {gp['vo_peak_v']} V > {prot['ovp_hw']['value']:.1f} V → 必须依赖 OVP/TVS 防护", warn_only=True, ref="PROT")

    # --- 保护阈值次序 ---
    tvs, _ = selected(sel, db, "tvs")
    cb, _ = selected(sel, db, "cout_bulk")
    cc, _ = selected(sel, db, "cout_ceramic")
    q, _ = selected(sel, db, "mosfet")
    vbr = param(tvs, "vbr_min_v", ("min", "typ")) if tvs else None
    vcl = param(tvs, "vc_v", ("max", "typ")) if tvs else None
    cap_ratings = [param(part, "voltage_v") if part else None for part in (cb, cc)]
    v_caps = min(cap_ratings) if all(v is not None for v in cap_ratings) else None
    vds = param(q, "vds_max_v") if q else None
    chain = [vo, prot["ovp_sw"]["value"], prot["ovp_hw"]["value"]] + ([vbr] if vbr else [])
    K.add("SR30", "阈值次序 Vo < OVPsw < OVPhw < TVS VBR,min",
          None if vbr is None else all(a < b for a, b in zip(chain, chain[1:])),
          " < ".join(f"{v:.1f}" for v in chain) if vbr is not None else "TVS VBR,min 缺失",
          ref="PROT")
    K.add("SR31", "TVS 钳位电压 ≤ MOSFET Vds 额定",
          None if vcl is None or vds is None else vcl <= vds,
          f"VC={vcl} V vs Vds={vds} V" if vcl is not None and vds is not None else "TVS/MOSFET 额定参数缺失",
          warn_only=True, ref="PROT")
    K.add("SR32", "TVS 钳位电压 ≤ 输出电容耐压（仅手册脉冲条件对照）",
          None if vcl is None or v_caps is None else vcl <= v_caps,
          f"VC={vcl} V vs {v_caps} V；未计温漂与布局尖峰，另见 SR35"
          if vcl is not None and v_caps is not None else "TVS/输出电容额定参数缺失",
          warn_only=True, ref="PROT")
    K.add('SR35', 'TVS 全条件保护配合已验证', None if tvs is None else False,
          '24.4V 仅对应手册16.4A、10/1000µs脉冲；25V电容裕量小。温度、布线寄生与故障能量未验证。',
          warn_only=True, ref='PROT')
    K.add("SR33", "OCP 次序 Icc < OCPsw < OCPhw", io < prot["ocp_sw"]["value"] < prot["ocp_hw"]["value"],
          f"{io} < {prot['ocp_sw']['value']} < {prot['ocp_hw']['value']} A", ref="PROT")
    ind, _ = selected(sel, db, "inductor")
    il_ocp = prot["ocp_hw"]["value"] / (1 - env['D_max'])
    isat = param(ind, "isat_a", ("min", "typ")) if ind else None
    ocp = [c for c in sim_cases if "SR87" in c["checks"]]
    ocp_evidence = (f"输出 OCP 仿真 {sum(c['checks']['SR87'] is True for c in ocp)}/{len(ocp)} 通过。"
                    if ocp else "")
    K.add("SR34", "故障电感峰值及输入保护配合已证明", None if isat is None else False,
          f"{ocp_evidence}仅稳态换算输出OCPhw对应IL≈{il_ocp:.2f} A vs Isat={isat} A；不是故障上界。无电感逐周期限流，"
          "输出OCP不能保证避免电感饱和；独立输入限流的瞬态与短路耐受尚未验证。", warn_only=True, ref="PROT")
    irms = param(ind, 'irms_a', ('min','typ')) if ind else None
    K.add('SR36', '额定容差场景电感峰值低于手册Isat',
          None if isat is None else env['IL_pk_max_A'] < isat,
          f"ILpk={env['IL_pk_max_A']} A vs Isat={isat} A；非故障工况", ref='PWR')
    K.add('SR37', '额定容差场景电感RMS低于手册温升电流',
          None if irms is None else env['IL_rms_max_A'] < irms,
          f"ILrms={env['IL_rms_max_A']} A vs Irms={irms} A；适用手册温升条件", ref='PWR')
    K.add('SR38', '电感实际纹波比满足内部设计目标', env['ripple_ratio_max'] <= ch['power_stage']['inductor_ripple_ratio'],
          f"容差场景最大{env['ripple_ratio_max']} vs 内部目标{ch['power_stage']['inductor_ripple_ratio']}；非课程硬指标", warn_only=True, ref='DEC-L')
    bulk_rms = max(x['capacitor_branch_rms_A'][-1] for x in loss['corner_cases'])
    bulk_ma = param(cb, 'ripple_current_ma_100khz', ('max','typ')) if cb else None
    bulk_rating = bulk_ma / 1000 if bulk_ma is not None else None
    K.add('SR39', '输出储能电容支路纹波电流低于手册额定',
          None if bulk_rating is None else bulk_rms < bulk_rating,
          f'支路谐波RMS最大{bulk_rms:.4g} A vs {bulk_rating} A@100kHz；模型未含尖峰和漏电', ref='PWR')

    # --- 采样 ---
    s = calc["sensing"]
    vref, full = s["adc_vref_v"], 2 ** s["adc_bits"] - 1
    K.add("SR40", "Vout 采样在硬件 OVP 处不超量程", prot["ovp_hw"]["adc_code"] < 0.97 * full,
          f"code {prot['ovp_hw']['adc_code']} / {full}", ref="IF-ADC")
    K.add("SR41", "Iout 采样在硬件 OCP 处不超量程", prot["ocp_hw"]["adc_code"] < 0.97 * full,
          f"code {prot['ocp_hw']['adc_code']} / {full}", ref="IF-ADC")
    K.add("SR42", "Vout 分辨率 ≤ 0.1% Vo", s["vout"]["lsb_mv"] / 1000 <= 0.001 * vo, f"{s['vout']['lsb_mv']} mV", ref="IF-ADC")
    K.add("SR43", "Iout 分辨率 ≤ 0.2% Icc", s["iout"]["lsb_ma"] / 1000 <= 0.002 * io, f"{s['iout']['lsb_ma']} mA", ref="IF-ADC")
    csa, _ = selected(sel, db, "csa")
    vos = param(csa, "vos_uv", ("max", "typ")) if csa else None
    ge = param(csa, "gain_error_pct", ("max", "typ")) if csa else None
    if vos is not None and ge is not None:
        err = vos * 1e-6 / (s["iout"]["shunt_ohm"] * io) * 100 + ge + 1.0  # + 分流电阻 1%
        K.add("SR44", "电流测量部分误差预算 ≤ 稳态精度要求的一半", err <= req["iout"]["steady_err_pct"] / 2,
              f"失调 + 增益误差 + 分流 1% ≈ {err:.2f}% vs ≤{req['iout']['steady_err_pct']/2}%；"
              "失调条件为12V共模，未含ADC/基准/温漂/CMRR，非完整最坏保证；须在实际共模下标定。",
              warn_only=True, ref="REQ-ACC")
    else:
        K.add("SR44", "电流测量部分误差预算 ≤ 稳态精度要求的一半", None,
              "未选电流放大器，或 vos_uv/gain_error_pct 缺失；不得用零替代", ref="REQ-ACC")
    verr = 2 * 1.0 + 0.5  # 两只 1% 电阻 + ADC/基准
    K.add("SR45", "电压测量规划预算 ≤ 稳态精度要求的一半", verr <= req["vout"]["steady_err_pct"] / 2,
          f"≈{verr}%（1%电阻×2 + ADC/基准0.5%占位预算，非已验证上界）；另见SR46", warn_only=True, ref="REQ-ACC")
    K.add('SR46', '电压/电流完整误差与硬件阈值容差已闭合', False,
          'ADC、基准、温漂、共模范围和阈值源须补预算；ADC等效码不是DAC码；独立DAC/COMP/BKIN及ADC并用路由尚待核实。', warn_only=True, ref='IF-ADC')

    # --- 效率 / 热 / 纹波 ---
    eta = loss["cases"]["nom"]["eta_total"] * 100
    K.add("SR50", "额定点效率估算 ≥ 课程建议目标", eta >= req["efficiency"]["target_pct"],
          f"{eta:.2f}% vs target {req['efficiency']['target_pct']}%（未实测）", warn_only=True, ref="REQ-EFF")
    K.add('SR50-margin', '容差场景效率估算 ≥ 课程目标+5个百分点内部裕量', 100*env['eta_min'] >= req['efficiency']['target_pct']+5,
          f"最低{100*env['eta_min']:.2f}%（{env['eta_min_case']}）；+5个百分点为内部目标", warn_only=True, ref='DEC-EFF')
    for n,t in env['tj_max_C'].items():
        category = {'MOSFET': 'mosfet', '二极管': 'diode', '防反接 PMOS': 'pmos'}.get(n)
        if category and sel['categories'].get(category, {}).get('selected') is None:
            K.not_applicable(f'SR51-{n}', f'{n}容差场景估计结温 < 100°C',
                             f'未选用 {category}；相应的选型完整性另由 SR10 检查', ref='THERM')
        else:
            part, _ = selected(sel, db, category) if category else (None, 0)
            assessed = t is not None and (not category or part is not None)
            K.add(f'SR51-{n}', f'{n}容差场景估计结温 < 100°C', None if not assessed else t < 100,
                  f"最大{t}°C，Ta={loss['assumptions']['ambient_c']}°C；依赖热阻/铜面积假设，非实测", ref='THERM')
    rp = loss["ripple"]["ripple_pct"]
    K.add("SR52", "输出纹波估算 ≤ 课程硬性上限", rp <= req["ripple"]["max_pct"], f"{rp}% vs {req['ripple']['max_pct']}%（不含ESL/开关尖峰）", ref="REQ-RIP")
    K.add('SR52-target', '输出纹波估算 ≤ 课程建议值', rp <= req['ripple']['target_pct'],
          f"{rp}% vs target {req['ripple']['target_pct']}%", warn_only=True, ref='REQ-RIP')
    ramp_cases = [c for c in sim_cases if "SR88" in c["checks"]]
    ramp_evidence = (f"独立输入缓升仿真 {sum(c['checks']['SR88'] is True for c in ramp_cases)}/{len(ramp_cases)} 通过，"
                     f"电源峰值预算估算最高 {max(c['metrics']['source_peak_bound_a'] for c in ramp_cases):.3f} A。"
                     if ramp_cases else "")
    K.add("SR53", "上电浪涌限制措施已验证", loss["inrush"]["mitigation_verified"],
          f"{ramp_evidence}无保护线性峰值{loss['inrush']['i_peak_a']}A，超过Isat时不是可靠上界。"
          "待验证独立缓升硬件斜率、电源释放能量、PWM禁止及SOA。", warn_only=True, ref="PROT")

    fi = loss["inrush"].get("fuse_melting_i2t_a2s")
    K.add("SR54", "保险丝nominal I²t与线性浪涌模型对照 ≥ 10倍", None if fi is None else fi >= 10 * loss["inrush"]["i2t_a2s"],
          f"熔断nominal {fi} A²s vs 线性浪涌{loss['inrush']['i2t_a2s']} A²s；非不熔断/重复寿命/短路保护配合证明", warn_only=True, ref="PROT")

    # --- 控制 ---
    for m in ("CV", "CC"):
        r = loop[m]
        K.add(f"SR60-{m}", f"{m} 环路在已建模CCM工况满足 PM≥{ch['control']['pm_min_deg']}°, GM≥{ch['control']['gm_min_db']} dB",
              r["ok"], f"Kp={r.get('Kp')}, Ki={r.get('Ki')}, 最差 fc={r.get('fc_min_Hz')} Hz", ref="CTRL")
        bad = [x for x in r.get('excluded_corners',[]) if not x['error'].startswith('DCM')]
        K.add(f'SR60-reachable-{m}', f'{m}指定工况均可达', not bad, f'不可达点{len(bad)}', ref='CTRL')
    dcm_points = len(loop["CV"].get("excluded_corners", []))
    if sim:
        dynamic = [c for c in sim["cases"] if c["name"].startswith(("S-light", "S-handover"))]
        covered = (f"开关仿真覆盖 {len(dynamic)} 个轻载/切换工况，"
                   f"{sum(c['verdict'] == 'PASS' for c in dynamic)} 个通过；仍需实测。")
    else:
        covered = "需开关仿真和实测。"
    K.add('SR65', 'DCM/空载/停脉冲/CC-CV切换/饱和已验证', False if sim else None,
          f"CV有{dcm_points}个DCM点排除于CCM搜索；{covered}", warn_only=True, ref='CTRL')
    rules = {
        "SR80": "启动过冲 < 10%，进入 ±2% 带且无故障锁存",
        "SR81": "稳态恒压与恒流误差满足需求",
        "SR82": "开关仿真输出纹波满足硬性上限",
        "SR82-target": "开关仿真输出纹波满足建议值",
        "SR83": "CV/CC 切换后收敛且无持续波动",
        "SR84": "轻载及空载末段电压波动不超过 1%",
        "SR85": "卸载过压峰值低于 TVS 最小击穿电压",
        "SR87": "输出过流仿真验证无误触发、软件延时、硬件关断及锁存",
    }
    if ch["protection"].get("input_ramp"):
        rules["SR88"] = "独立输入缓升下的冷插电电感及电源峰值满足预算"
    else:
        K.not_applicable("SR88", "独立输入缓升下的冷插电电流满足预算",
                         "design_choices.protection 未配置 input_ramp；未验证缓升保护，另见 SR53", ref="SIM")
    for rule_id, title in rules.items():
        applicable = [c for c in sim_cases if rule_id in c["checks"]]
        failed = [c["waveform"] for c in applicable if c["checks"][rule_id] is False]
        missing = [c["waveform"] for c in applicable if c["checks"][rule_id] is None]
        detail = f"{len(applicable)} 个工况，失败 {len(failed)}，缺数据 {len(missing)}"
        if failed or missing:
            detail += "；" + "、".join(failed + missing)
        K.add(rule_id, title, False if failed else (None if not applicable or missing else True), detail,
              warn_only=rule_id == "SR82-target" or not failed, ref="SIM")
    cross = sim.get("cross_validation", {}) if sim else {}
    expected_cross = {"duty", "il_avg", "ripple_mv", "worst_ripple_mv"}
    complete_cross = expected_cross.issubset(cross) and all(cross[k].get("passed") is not None for k in expected_cross)
    K.add("SR86", "额定点开关仿真与平均模型交叉验证", None if not complete_cross else all(cross[k]["passed"] for k in expected_cross),
          "；".join(f"{key}: {value['error_pct']:.2f}% / {value['limit_pct']:g}%"
                   for key, value in cross.items()) if complete_cross else "缺少额定交叉验证输入或结果", ref="SIM")
    K.add("SR61", "PWM 分辨率 ≥ 10 bit", calc["pwm"]["resolution_bits"] >= 10, f"{calc['pwm']['resolution_bits']} bit", ref="CTRL")
    K.add("SR62", "控制中断 CPU 占用 < 50%", calc["timing"]["cpu_load_pct"] < 50, f"{calc['timing']['cpu_load_pct']}%", ref="FW")
    K.add("SR63", "日志串口占用 < 50%", calc["timing"]["uart_load_pct"] < 50, f"{calc['timing']['uart_load_pct']}%", ref="FW")
    K.add("SR64", "CC 可调范围与短路边界已说明", "short_circuit" in calc["cc_range"],
          f"CR {calc['cc_range']['r_min_ohm']}–{calc['cc_range']['r_max_ohm']} Ω", ref="REQ-CC")

    # --- 供电 ---
    ldo, _ = selected(sel, db, "ldo")
    drv, _ = selected(sel, db, "gate_driver")
    # Both auxiliary loads are supplied after the fuse and reverse-polarity
    # MOSFET. Nominal fuse resistance and typical dropout are useful estimates,
    # but cannot close a check that requires maximum parameter values.
    pm, _ = selected(sel, db, "pmos")
    fu, _ = selected(sel, db, "fuse")
    def bound(part, name, field):
        # An unqualified scalar is not evidence of a guaranteed limit.
        value = (part or {}).get("params", {}).get(name)
        return value.get(field) if isinstance(value, dict) else None

    rpm = bound(pm, "rds_on_mohm_vgs4v5", "max")
    rfuse = bound(fu, "r_mohm", "max")
    path_complete = rpm is not None and rfuse is not None
    vbus = None
    if path_complete:
        r_path = (rpm * loss["model"]["rds_hot_factor"] + rfuse) / 1000
        # Use the input-current envelope, not only the nominal min-Vin case.
        iin = max(c["iin_avg_A"] for c in loss["corner_cases"])
        vbus = min(env['driver_vdd_min_v'], vin["min"] - iin * r_path)
    vdo = bound(ldo, "dropout_mv", "max")
    vdo_typ = param(ldo, "dropout_mv", ("typ",)) if ldo else None
    K.add("SR70", "LDO 压差余量 ≥ 200 mV @Vin,min",
          None if vdo is None or vbus is None else (vbus - 3.3) * 1000 - vdo >= 200,
          f"保护后 VIN={vbus:.3f} V，压差余量 {(vbus-3.3)*1000-vdo:.0f} mV；"
          "限已建模电流场景，温度系数和布线压降仍为模型假设"
          if vdo is not None and vbus is not None else
          f"缺少 LDO dropout.max 或输入路径电阻 max；dropout.typ={vdo_typ} mV，"
          "典型值不构成保证；LDO 接保护后 VIN，不能直接用端子 Vin,min 判 PASS", ref="PWR-AUX")
    if drv:
        uv = bound(drv, "uvlo_on_v", "max")
        vmin = bound(drv, "vdd_min_v", "min")
        need = max(uv, vmin) if uv is not None and vmin is not None else None
        K.add("SR71", "驱动器最低供电 > max(UVLO 开启最大值, 保证工作下限) + 0.1 V",
              None if need is None or vbus is None else vbus >= need + 0.1,
              f"保护后 VDD={vbus:.3f} V vs 工作下限 {need} V + 0.1 V；"
              "限已建模电流场景，温度系数和布线压降仍为模型假设"
              if need is not None and vbus is not None else
              "缺少 UVLO.max、工作下限 min 或 PMOS/保险丝电阻 max；"
              f"现有模型估算 {env['driver_vdd_min_v']} V 不构成最坏供电保证", ref="PWR-AUX")
    else:
        K.add("SR71", "驱动器最低供电 > max(UVLO 开启最大值, 保证工作下限) + 0.1 V",
              None, "未选栅极驱动器", ref="PWR-AUX")
    K.add("SR72", "η 假设与复核一致", not loss["eta_consistency"]["needs_iteration"], loss["eta_consistency"]["advice"],
          warn_only=True, ref="ITER")

    K.add('SR73', '模型假设与实物功率/采样时序已验证', False,
          '开关/磁损、热Rds系数、MLCC保持率、陶瓷ESR、ESR敏感性范围及热布局均为估计；放大器典型带宽非最坏保证；ADC窗口/CPU最坏执行时间需B/C/D实测。', warn_only=True, ref='MODEL')
    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    _decorate_checks(K.items, rules)
    summ = K.summary()
    hard_missing = any(c["verdict"] == "NOT_ASSESSED" and c["severity"] in ("critical", "major")
                       for c in K.items)
    blocking_warning = any(c["verdict"] == "WARN" and c["verify"] == "automatic"
                           and c["severity"] in ("critical", "major") for c in K.items)
    if summ["FAIL"] or blocking_warning:
        status = "needs_rework"
    elif hard_missing:
        status = "needs_rework"
    elif summ["WARN"] or summ["NOT_ASSESSED"]:
        status = "conditional_review"
    else:
        status = "ready_for_downstream"
    return {"schema": "boost-sdrc/2", "revision": ch.get("revision"), "summary": summ,
            "status_policy": "FAIL/critical-major missing/automatic critical-major WARN block readiness; minor/info automatic WARN remain explicit advisories",
            "status": status, "checks": K.items}


def main():
    ap = argparse.ArgumentParser(description=__doc__ + "\n退出码：0=无 FAIL 且无 critical/major NOT_ASSESSED；2=存在 FAIL；3=存在 critical/major NOT_ASSESSED；1=程序错误。",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir")
    ap.add_argument("--db", default=str(KNOWLEDGE_DIR / "parts_db.json"))
    a = ap.parse_args()
    p = workdir_paths(a.workdir)
    sim = load_json(p["sim"]) if p["sim"].exists() else None
    res = run(*(load_json(p[k]) for k in ("requirements", "choices", "calc", "selection", "losses", "loop")),
              load_json(a.db), sim)
    save_json(p["check"], res)
    print(f"SDRC: {res['summary']} → {res['status']}")
    for c in res["checks"]:
        if c["verdict"] != "PASS":
            print(f"  [{c['verdict']}] {c['id']} {c['rule']}: {c['detail']}")
    sys.exit(_exit_code(res))


if __name__ == "__main__":
    main()
