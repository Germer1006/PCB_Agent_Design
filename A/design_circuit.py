"""Calculate a supported non-Boost circuit and export its schematic intent."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path



def positive_number(requirement, key):
    value = requirement[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} must be a positive finite number")
    return value


def design_rc(requirement):
    required = {"revision", "topology", "channels", "cutoff_hz", "nominal_tolerance_pct",
                "resistance_ohm", "capacitor_options_nf"}
    if not isinstance(requirement, dict) or set(requirement) != required:
        raise ValueError("RC requirements must have exactly: " + ", ".join(sorted(required)))
    revision = requirement["revision"]
    channels = requirement["channels"]
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("Nonempty revision required")
    if type(channels) is not int or not 1 <= channels <= 8:
        raise ValueError("channels must be an integer from 1 to 8")
    target = positive_number(requirement, "cutoff_hz")
    tolerance = positive_number(requirement, "nominal_tolerance_pct")
    resistance = positive_number(requirement, "resistance_ohm")
    caps = requirement["capacitor_options_nf"]
    if not isinstance(caps, list) or not caps:
        raise ValueError("capacitor_options_nf needs at least one candidate")
    options = [positive_number({"capacitance": cap}, "capacitance") for cap in caps]
    evaluated = [(1 / (2 * math.pi * resistance * cap * 1e-9), cap) for cap in options]
    if any(not math.isfinite(cutoff) or cutoff <= 0 for cutoff, _ in evaluated):
        raise ValueError("Capacitor options are outside the numeric range")
    cutoff, cap = min(evaluated, key=lambda pair: (abs(pair[0] - target), pair[1]))
    error = 100 * abs(cutoff / target - 1)
    if error > tolerance:
        raise ValueError(f"No capacitor option meets nominal cutoff tolerance; closest is {cutoff:.5g} Hz")

    components, nets, groups = [], [], []
    def part(ref, symbol, value, footprint):
        components.append({"ref": ref, "lib_id": symbol, "value": value, "footprint": footprint})

    connector = f"Connector_PinHeader_2.54mm:PinHeader_1x{channels + 1:02d}_P2.54mm_Vertical"
    symbol = f"Connector_Generic:Conn_01x{channels + 1:02d}"
    part("J1", symbol, "Signal input", connector)
    part("J2", symbol, "Filtered output", connector)
    return_pin = str(channels + 1)
    return_pins = [["J1", return_pin], ["J2", return_pin]]
    for index in range(1, channels + 1):
        pin = str(index)
        resistor, capacitor = f"R{index}", f"C{index}"
        part(resistor, "Device:R", f"{resistance:g} ohm", "Resistor_SMD:R_0603_1608Metric")
        part(capacitor, "Device:C", f"{cap:g} nF", "Capacitor_SMD:C_0805_2012Metric")
        nets.extend([
            {"name": f"AIN_{index}", "pins": [["J1", pin], [resistor, "1"]]},
            {"name": f"FILT_{index}", "pins": [[resistor, "2"], [capacitor, "1"], ["J2", pin]]},
        ])
        return_pins.append([capacitor, "2"])
        groups.append({"id": f"rc_channel_{index}", "refs": [resistor, capacitor],
                       "max_span_mm": None, "evidence": ["Filter resistor and capacitor share the filtered node."]})
    nets.append({"name": "RET_ANALOG", "pins": return_pins})
    open_issues = ["Review source impedance, load impedance and component tolerances against the cutoff target.",
                   "Confirm component voltage ratings, footprints and the physical PCB before prototype release."]
    calculation = {"cutoff_hz": cutoff, "nominal_error_pct": error, "resistance_ohm": resistance,
                   "capacitance_nf": cap, "model": "First-order RC with ideal source and unloaded output."}
    return draft_spec(revision, "rc_lowpass", calculation, components, nets, groups, open_issues)


def draft_spec(revision, topology, calculation, components, nets, groups, open_issues):
    return {
        "revision": revision, "topology": topology, "status": "needs_review",
        "formal_handoff": False, "open_issues": open_issues, "calculation": calculation,
        "schematic_intent": {"schema_version": "schematic-intent-1", "components": components, "nets": nets},
        "pcb_constraints": {"schema_version": "pcb-constraints-1", "design_revision": revision,
                            "upstream": {"status": "needs_review", "open_issues": open_issues},
                            "groups": groups, "ground_plane_net": "/RET_ANALOG",
                            "net_roles": {"/" + net["name"]: ("ground" if net["name"] == "RET_ANALOG" else "sense")
                                          for net in nets}},
    }


def design_divider(requirement):
    required = {"revision", "topology", "max_input_v", "target_output_v", "max_output_v",
                "nominal_tolerance_pct", "resistor_options_ohm"}
    if set(requirement) != required:
        raise ValueError("Divider requirements must have exactly: " + ", ".join(sorted(required)))
    revision = requirement["revision"]
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("Nonempty revision required")
    vin = positive_number(requirement, "max_input_v")
    target = positive_number(requirement, "target_output_v")
    ceiling = positive_number(requirement, "max_output_v")
    tolerance = positive_number(requirement, "nominal_tolerance_pct")
    if target >= vin or target > ceiling:
        raise ValueError("Target output must be below input and at or below the output limit")
    options = requirement["resistor_options_ohm"]
    if not isinstance(options, list) or not options:
        raise ValueError("Provide resistor options")
    resistors = sorted({positive_number({"resistance": value}, "resistance") for value in options})
    candidates = []
    for upper in resistors:
        for lower in resistors:
            voltage = vin / (1 + upper / lower)
            if 0 < voltage <= ceiling:
                candidates.append((abs(voltage / target - 1), upper, lower, voltage))
    if not candidates or min(candidates)[0] * 100 > tolerance:
        raise ValueError("No resistor pair meets the nominal output range")
    error, upper, lower, voltage = min(candidates)
    connector = "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
    symbol = "Connector_Generic:Conn_01x02"
    components = [{"ref": ref, "lib_id": symbol, "value": title, "footprint": connector}
                  for ref, title in (("J1", "Sense input"), ("J2", "Divider output"))]
    components.extend({"ref": ref, "lib_id": "Device:R", "value": f"{value:g} ohm",
                       "footprint": "Resistor_SMD:R_0603_1608Metric"}
                      for ref, value in (("R1", upper), ("R2", lower)))
    nets = [{"name": "MEAS_INPUT", "pins": [["J1", "1"], ["R1", "1"]]},
            {"name": "MEAS_OUTPUT", "pins": [["R1", "2"], ["R2", "1"], ["J2", "1"]]},
            {"name": "RET_ANALOG", "pins": [["J1", "2"], ["J2", "2"], ["R2", "2"]]}]
    calculation = {"output_at_max_input_v": voltage, "nominal_error_pct": error * 100, "upper_ohm": upper,
                   "lower_ohm": lower, "model": "Ideal source and unloaded output."}
    issues = ["Review resistor tolerances, ADC input loading, settling and maximum pin voltage.",
              "Confirm resistor voltage/power ratings and physical footprints before prototype release."]
    return draft_spec(revision, "voltage_divider", calculation, components, nets, [], issues)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requirements", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        requirements = json.loads(args.requirements.read_text(encoding="utf-8-sig"))
        models = {"rc_lowpass": design_rc, "voltage_divider": design_divider}
        topology = requirements.get("topology") if isinstance(requirements, dict) else None
        if not isinstance(topology, str) or topology not in models:
            raise ValueError("Use rc_lowpass or voltage_divider here. Use the existing Boost workflow for async_boost.")
        spec = models[topology](requirements)
        # Exclusive creation preserves existing handoffs.
        with args.output.open("x", encoding="utf-8") as out:
            json.dump(spec, out, ensure_ascii=False, indent=2)
            out.write("\n")
    except (ValueError, OSError, KeyError, OverflowError, ZeroDivisionError) as error:
        parser.exit(2, f"Design blocked: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
