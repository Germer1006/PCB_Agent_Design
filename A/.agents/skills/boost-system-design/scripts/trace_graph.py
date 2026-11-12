"""Cross-stage dependencies for the ledger, SDRC and handoff fields."""

from collections import defaultdict, deque


# These values are read from the rounded JSON results at stage boundaries.
CALC_FIELDS = {
    "inputs.vin": ("P3", "P3_MAX"),
    "inputs.vin.min": ("P3",),
    "inputs.vin.max": ("P3_MAX",),
    "inputs.pout_w": ("P1",),
    "inductor.l_selected_uh": ("L2",),
    "output_cap.c_eff_required_uf": ("C1",),
    "sensing.iout.shunt_ohm": ("S4",),
    "sensing.vout.divider_loss_w": ("S1",),
    "sensing.vout.filter_fc_hz": ("S1_FILT",),
    "sensing.vin.r_top_ohm": ("S3",),
    "sensing.vin.r_bot_ohm": ("S3",),
    "aux.i_3v3_ma": ("A1",),
    "cc_range.r_min_ohm": ("CC2",),
}
RAW_CALC_FIELDS = {
    "inputs.vin": ("req:vin.nom_v",),
    "inputs.vout_v": ("req:vout.set_v",),
    "inputs.iout_cc_a": ("req:iout.cc_set_a",),
    "inputs.fsw_hz": ("choice:fsw_hz",),
}

# Inputs used by each numerical selection policy in select_parts.py.
SELECTION_STEPS = {
    "inductor": ("L2", "L6", "L7"),
    "mosfet": ("Q1", "Q2", "L4"),
    "diode": ("D3", "D4", "L4"),
    "cout_bulk": ("C2", "C3", "C4"),
    "cout_ceramic": ("C1", "C4"),
    "cin_ceramic": ("C5", "P3_MAX"),
    "cin_bulk": ("P3_MAX",),
    "pmos": ("P3", "P3_MAX", "I1"),
    "fuse": ("P3_MAX", "I1"),
    "gate_driver": ("P3", "P3_MAX", "T1"),
    "csa": ("S4",),
    "shunt": ("S4",),
    "ldo": ("A1", "A2", "A3"),
    "tvs": ("PR2",),
    "mcu": ("T1", "T3", "S2", "S5"),
    "terminal": ("P3_MAX", "PR2", "I1", "D4"),
}

# Rule dependencies follow check_design.py and the modeled evidence it reads.
RULE_STEPS = {
    "SR01": ("P1",), "SR02": ("P3_MAX",), "SR03": ("T1",),
    "SR20": ("D7", "L_PK_MAX"), "SR21": ("D7", "G1"), "SR22": ("D7", "G1", "PR2"),
    "SR30": ("PR1", "PR2"), "SR31": ("Q1",), "SR32": ("C4",),
    "SR33": ("PR3", "PR4"), "SR34": ("PR4", "L_PK_MAX"),
    "SR36": ("L_PK_MAX",), "SR37": ("L_RMS_MAX",), "SR38": ("L_RIPPLE_MAX", "L1"),
    "SR39": ("R2",), "SR40": ("S1", "PR2"), "SR41": ("S4", "PR4"),
    "SR42": ("S2",), "SR43": ("S5",), "SR44": ("S4",),
    "SR45": ("S1",), "SR46": ("S1", "S4"),
    "SR50": ("E2",), "SR50-margin": ("E4",), "SR51-MOSFET": ("TJ_MOSFET",),
    "SR51-二极管": ("TJ_DIODE",), "SR51-防反接 PMOS": ("TJ_PMOS",),
    "SR52": ("R3",), "SR52-target": ("R3",), "SR53": ("U1",), "SR54": ("U2",),
    "SR60-CV": ("CV_KP", "CV_KI"), "SR60-CC": ("CC_KP", "CC_KI"),
    "SR60-reachable-CV": ("CV_KP",), "SR60-reachable-CC": ("CC_KP",),
    "SR61": ("T1",), "SR62": ("T4",), "SR63": ("T5",), "SR64": ("CC2",),
    "SR65": ("CV_KP", "CC_KP", "L8"), "SR70": ("A3", "A1"), "SR71": ("L_PK_MAX",),
    "SR72": ("E2",), "SR73": ("E4", "CV_KP", "CC_KP"),
    "SR86": ("E1", "R2"),
}
RULE_ROLES = {
    "SR05": ("mcu",), "SR30": ("tvs",), "SR31": ("tvs", "mosfet"),
    "SR32": ("tvs", "cout_bulk", "cout_ceramic"), "SR35": ("tvs",),
    "SR34": ("inductor",), "SR36": ("inductor",), "SR37": ("inductor",),
    "SR39": ("cout_bulk",), "SR44": ("csa", "shunt"), "SR46": ("mcu", "csa", "shunt"),
    "SR53": ("inductor", "pmos", "fuse"), "SR54": ("fuse",),
    "SR70": ("ldo",), "SR71": ("gate_driver",), "SR85": ("tvs",), "SR88": ("inductor",),
}

HANDOFF_FIELDS = {
    "design_spec.json.control.pwm_arr": ("T1",), "boost_params.h.PWM_ARR": ("T1",),
    "design_spec.json.control.pwm_ccr_max": ("T1", "D7"), "boost_params.h.PWM_CCR_MAX": ("T1", "D7"),
    "design_spec.json.control.f_ctrl_hz": ("T3",), "boost_params.h.CTRL_FREQ_HZ": ("T3",),
    "design_spec.json.topology.fsw_hz": ("T1",), "boost_params.h.PWM_FSW_HZ": ("T1",),
    "boost_params.h.PWM_TIM_CLK_HZ": ("T1",), "boost_params.h.CTRL_TS_S": ("T3",),
    "design_spec.json.control.tim1_rcr": ("T1", "T3"), "boost_params.h.TIM1_RCR": ("T1", "T3"),
    "design_spec.json.sensing.vout": ("S1", "S1_FILT", "S2"),
    "design_spec.json.sensing.vin": ("S3",), "design_spec.json.sensing.iout": ("S4", "S5"),
    "boost_params.h.VOUT_V_PER_CODE": ("S2",), "boost_params.h.VIN_V_PER_CODE": ("S3",),
    "boost_params.h.IOUT_A_PER_CODE": ("S5",),
    "boost_params.h.IOUT_SHUNT_OHM": ("S4",), "boost_params.h.IOUT_CSA_GAIN": ("S4",),
    "design_spec.json.interfaces[IF-01]": ("T1", "D7"),
    "design_spec.json.interfaces[IF-02]": ("E1",),
    "design_spec.json.interfaces[IF-03]": ("S1", "S2"),
    "design_spec.json.interfaces[IF-04]": ("S4", "S5"),
    "design_spec.json.interfaces[IF-05]": ("S3",),
    "design_spec.json.interfaces[IF-06]": ("A1",),
    "design_spec.json.interfaces[IF-08]": ("E1", "CC2", "P2"),
    "design_spec.json.protection.ovp_sw": ("PR1", "S1"),
    "design_spec.json.protection.ovp_hw": ("PR2", "S1"),
    "design_spec.json.protection.ocp_sw": ("PR3", "S4"),
    "design_spec.json.protection.ocp_hw": ("PR4", "S4"),
    "boost_params.h.OVP_SW_V": ("PR1",), "boost_params.h.OVP_HW_V": ("PR2",),
    "boost_params.h.OCP_SW_A": ("PR3",), "boost_params.h.OCP_HW_A": ("PR4",),
    "boost_params.h.OVP_SW_ADC_EQ_CODE": ("PR1", "S1"), "boost_params.h.OVP_HW_ADC_EQ_CODE": ("PR2", "S1"),
    "boost_params.h.OCP_SW_ADC_EQ_CODE": ("PR3", "S4"), "boost_params.h.OCP_HW_ADC_EQ_CODE": ("PR4", "S4"),
    "design_spec.json.cc_range": ("P2", "CC1", "CC2"),
    "boost_params.h.CC_VOUT_MIN_V": ("CC1",), "boost_params.h.CC_CR_MIN_OHM": ("CC2",),
    "boost_params.h.CC_CR_MAX_OHM": ("P2",),
    "boost_params.h.DUTY_MAX": ("D7",), "design_spec.json.control.duty_limit": ("D7",),
    "design_spec.json.protection.duty_limit": ("T1", "D7"),
    "design_spec.json.operating_point_nominal": ("E1", "E2", "E3"),
    "design_spec.json.control.cv_pi": ("CV_KP", "CV_KI", "CV_KI_TS"),
    "design_spec.json.control.cc_pi": ("CC_KP", "CC_KI", "CC_KI_TS"),
    "boost_params.h.CV_KP": ("CV_KP",), "boost_params.h.CV_KI_TS": ("CV_KI_TS",),
    "boost_params.h.CC_KP": ("CC_KP",), "boost_params.h.CC_KI_TS": ("CC_KI_TS",),
    "report_tables.md.表 D 损耗分解": ("E1", "E2"), "report_tables.md.表 F-CV": ("CV_KP", "CV_KI"),
    "report_tables.md.表 F-CC": ("CC_KP", "CC_KI"),
}
HANDOFF_SOURCES = {
    "boost_params.h.DUTY_MIN": ("choice:duty.dmin",),
    "design_spec.json.control.duty_limit": ("choice:duty.dmin",),
    "design_spec.json.protection.duty_limit": ("choice:duty.dmin",),
    "boost_params.h.SOFTSTART_MS": ("choice:control.soft_start_ms",),
    "design_spec.json.control.soft_start_ms": ("choice:control.soft_start_ms",),
    "design_spec.json.protection.soft_start": ("choice:control.soft_start_ms",),
    "boost_params.h.VOUT_SET_V": ("req:vout.set_v",),
    "boost_params.h.IOUT_SET_A": ("req:iout.cc_set_a",),
    "boost_params.h.CC_VOUT_MAX_V": ("req:vout.set_v",),
    "boost_params.h.ADC_FULL_SCALE": ("choice:sensing.adc_bits",),
    "boost_params.h.ADC_VREF_V": ("choice:sensing.adc_vref_v",),
    "boost_params.h.IOUT_FILTER_R_OHM": ("choice:losses.current_filter_r_ohm",),
    "boost_params.h.IOUT_FILTER_C_F": ("choice:losses.current_filter_c_f",),
    "design_spec.json.control.current_filter": ("choice:losses.current_filter_r_ohm",
                                                "choice:losses.current_filter_c_f", "selection:csa.selected"),
    "design_spec.json.protection.input_ramp": ("choice:protection.input_ramp",),
    "boost_params.h.OCP_SW_DELAY_MS": ("choice:protection.ocp_sw_delay_ms",),
    "boost_params.h.UVLO_OFF_V": ("choice:protection.uvlo_off_v",),
    "boost_params.h.UVLO_ON_V": ("choice:protection.uvlo_on_v",),
    "design_spec.json.protection.uvlo": ("choice:protection.uvlo_off_v", "choice:protection.uvlo_on_v", "step:S3"),
}

# Some requirements constrain a calculation or test without supplying a numerical operand.
REQUIREMENT_STEPS = {
    "REQ-IN": ("P3", "P3_MAX", "U1"), "REQ-CV": ("S2", "CV_KP", "CV_KI_TS"),
    "REQ-CC": ("CC1", "CC2", "S5", "CC_KP", "CC_KI_TS"),
    "REQ-PWR": ("P1", "L4", "L5", "L6", "L7", "Q1", "E1", "TJ_MOSFET", "TJ_DIODE", "TJ_PMOS"),
    "REQ-FSW": ("T1", "T2"), "TGT-FSW": ("T1",),
    "REQ-RIPPLE": ("C1", "C2", "R2", "R3"), "TGT-RIPPLE": ("C1", "C2", "R3"),
    "REQ-MCU": ("T1", "T3", "T4", "S2", "S5"), "TGT-MCU": ("T1", "T3"),
    "REQ-CTRL": ("D7", "T3", "CV_KP", "CV_KI_TS", "CC_KP", "CC_KI_TS"),
    "REQ-PROT": ("PR1", "PR2", "PR3", "PR4", "D7", "U1"), "TGT-START": ("T3",),
    "REQ-ETA-MEAS": ("E1", "E2"), "TGT-ETA": ("E2", "E4"),
}
REQUIREMENT_ROLES = {
    "REQ-IN": ("terminal", "fuse", "pmos", "cin_ceramic", "cin_bulk"),
    "REQ-CV": ("mcu",), "REQ-CC": ("csa", "shunt", "mcu"),
    "REQ-MCU": ("mcu",), "TGT-MCU": ("mcu",), "REQ-CTRL": ("mcu",),
    "REQ-FSW": ("mcu", "gate_driver", "mosfet"), "TGT-FSW": ("mcu", "gate_driver", "mosfet"),
    "REQ-PROT": ("tvs", "fuse", "pmos", "csa", "mcu"), "TGT-START": ("mcu",),
}
REQUIREMENT_RULES = {
    "REQ-IN": ("SR02", "SR53", "SR88"), "REQ-CV": ("SR81",), "REQ-CC": ("SR81",),
    "REQ-RIPPLE": ("SR52", "SR82"), "TGT-RIPPLE": ("SR52-target", "SR82-target"),
    "REQ-CTRL": ("SR60-CV", "SR60-CC", "SR83", "SR84"),
    "REQ-PROT": ("SR85", "SR87", "SR88"), "TGT-START": ("SR80",),
}


def build_graph(steps, selection, check):
    """Map each derived record to its immediate dependencies."""
    graph = defaultdict(set)
    for step in steps:
        graph["step:" + step["id"]].update(item["source"] for item in step["inputs"])
    known_steps = {step["id"] for step in steps}
    selected_roles = defaultdict(list)

    def link(node, ids):
        graph[node].update("step:" + sid for sid in ids if sid in known_steps)

    for path, ids in CALC_FIELDS.items():
        link("calc:" + path, ids)
    for path, sources in RAW_CALC_FIELDS.items():
        graph["calc:" + path].update(sources)
    for role, category in selection.get("categories", {}).items():
        node = f"selection:{role}.selected"
        link(node, SELECTION_STEPS.get(role, ()))
        if category["selected"]:
            graph[node].add("device:" + category["selected"])
            selected_roles[category["selected"]].append(node)
        graph[f"selection:{role}.qty"].add(node)
        for file, field in (("design_spec.json", "components"), ("bom_module1.csv", "role"), ("report_tables.md", "表 I")):
            graph[f"handoff:{file}.{field}[{role}]"].add(node)

    for step in steps:
        for item in step["inputs"]:
            source = item["source"]
            if source.startswith("part:"):
                pid = source.split(":", 1)[1].split(".", 1)[0]
                graph[source].add("device:" + pid)
                graph[source].update(selected_roles[pid])

    for rule in check.get("checks", ()):
        rid = rule["id"]
        node = "rule:" + rid
        link(node, RULE_STEPS.get(rid, ()))
        roles = RULE_ROLES.get(rid, ())
        if rid.startswith(("SR10-", "SR11-")):
            roles = (rid.split("-", 1)[1],)
        graph[node].update(f"selection:{role}.selected" for role in roles)
        if rule["ref"] == "SIM":
            link(node, ("CV_KI_TS", "CC_KI_TS", "T1", "D7", "PR1", "PR2", "PR3", "PR4"))
            graph[node].add("choice:protection.input_ramp")
        if rid == "SR38":
            graph[node].add("choice:power_stage.inductor_ripple_ratio")
        for file, field in (("report_tables.md", "表 G"), ("design_spec.json", "sdrc_summary")):
            graph[f"handoff:{file}.{field}"].add(node)
        if rule["verdict"] != "PASS":
            graph[f"handoff:design_spec.json.implementation_closures[{rid}]"].add(node)
    for field, ids in HANDOFF_FIELDS.items():
        link("handoff:" + field, ids)
    for field, sources in HANDOFF_SOURCES.items():
        graph["handoff:" + field].update(sources)
    for step in steps:
        table = {"calc": "B", "loss": "C", "loop": "F"}[step["stage"]]
        link(f"handoff:report_tables.md.表 {table}[{step['id']}]", (step["id"],))
    return {node: sorted(inputs) for node, inputs in sorted(graph.items())}


def reachable(graph, roots):
    found = set(roots)
    pending = deque(found)
    while pending:
        for node in graph.get(pending.popleft(), ()):
            if node not in found:
                found.add(node)
                pending.append(node)
    return found


def stale_records(graph, key):
    """Find affected steps, rules and handoff fields, including indirect dependents."""
    reverse = defaultdict(set)
    for node, inputs in graph.items():
        for source in inputs:
            reverse[source].add(node)
    path = key.removeprefix("choice:")
    roots = [node for node in reverse if node == f"device:{key}"
             or node.startswith("choice:") and
             (node == "choice:" + path or node.startswith(f"choice:{path}.") or node.endswith("." + path))]
    if not roots:
        raise ValueError(f"未找到器件或设计决策 {key!r}")
    affected = sorted(reachable(reverse, roots))
    return {label: [node.split(":", 1)[1] for node in affected if node.startswith(prefix)]
            for label, prefix in (("steps", "step:"), ("rules", "rule:"), ("handoff", "handoff:"))}


def extend_traceability(rows, calc, loss, loop, selection, check):
    steps = [{**step, "stage": stage} for stage, data in (("calc", calc), ("loss", loss), ("loop", loop))
             for step in data.get("steps", []) if step.get("inputs")]
    graph = build_graph(steps, selection, check)
    rule_ids = {item["id"] for item in check["checks"]}
    for row in rows:
        rid = row["req"]
        roots = [f"step:{sid}" for sid in REQUIREMENT_STEPS.get(rid, ()) if f"step:{sid}" in graph]
        roots += [f"selection:{role}.selected" for role in REQUIREMENT_ROLES.get(rid, ())]
        nodes = reachable(graph, roots)
        row["calculation_steps"] = [step["id"] for step in steps if "step:" + step["id"] in nodes]
        row["parts"] = sorted(node.split(":", 1)[1] for node in nodes if node.startswith("device:"))
        row["checks"] = list(dict.fromkeys(row["checks"] + [
            rule for rule in REQUIREMENT_RULES.get(rid, ()) if rule in rule_ids
        ]))
        if rid in ("REQ-BUILD", "REQ-SOURCE"):
            row["parts"] = sorted({v["selected"] for v in selection["categories"].values() if v["selected"]})
