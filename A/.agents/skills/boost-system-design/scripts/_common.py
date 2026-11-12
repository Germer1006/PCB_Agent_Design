"""Shared helpers for the boost-system-design tool scripts (standard library only)."""

import json
import math
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = SKILL_DIR / "knowledge"
TEMPLATE_DIR = SKILL_DIR / "templates"

E24 = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0,
       3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1]
E6 = [1.0, 1.5, 2.2, 3.3, 4.7, 6.8]
CALC_CONSTANTS = {
    "percent_divisor": 100, "uh_per_h": 1e6, "milli_per_unit": 1e3,
    "default_inductor_rounding": 0.0, "half_cycle": 2, "triangle_rms_divisor": 12,
    "input_ripple_divisor": 8, "uart_bits_per_byte": 10, "aux_rail_v": 3.3,
    "e24": E24, "e6": E6, "pi": math.pi,
    "ceramic_esr_ohm": 0.005, "default_hot_factor": 1.3,
    "default_inductor_dcr_mohm": 100, "default_mosfet_rds_mohm": 50,
    "default_capacitor_esr_mohm": 150, "default_output_esr_ohm": 0.01,
    "zero": 0, "default_diode_vf0_v": 0.45, "default_diode_rd_ohm": 0.05,
    "default_inductor_tolerance": .2, "default_bulk_cap_tolerance": .2,
    "default_ceramic_cap_tolerance": .1, "default_bulk_esr_sensitivity_factors": [.5, 1., 2.],
    "diode_fit_current_a": [0.5, 1.0, 2.0, 3.0, 5.0], "single_point_diode_split": [.85, .15],
    "loss_default": {"qg_nc": 8, "qgd_nc": 2, "qgs_nc": 1.3, "coss_pf": 80, "rg_ohm": 4.5,
                     "cj_pf": 500, "iq_ma": 1.9, "i_source_a": 1.3, "i_sink_a": 7.6,
                     "mosfet": 125, "diode": 80, "pmos": 125,
                     "rds_on_mohm_vgs4v5": 60, "r_mohm": 19},
    "ripple_harmonics": 60, "ripple_time_samples": 1024,
    "loop_frequency_grid": [0.1, .98, 420],
    "loop_kp_grid": [1e-3, 1.0, 31], "loop_ki_grid": [5, 2e4, 41],
    "loop_cv_loads": [2, 100.0, .2, .5, .9],
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def e_series(series, decades=range(-15, 10)):
    return sorted(m * 10 ** d for d in decades for m in series)


def pick_at_least(value, series=E24):
    """Smallest standard value >= value."""
    return min(v for v in e_series(series) if v >= value * (1 - 1e-9))


def pick_nearest(value, series=E6):
    return min(e_series(series), key=lambda v: abs(math.log(v / value)))


def ceil_to(x, step):
    return math.ceil(round(x / step, 9)) * step


def r3(x, n=4):
    """Round to n significant digits for readable JSON."""
    if x is None or x == 0 or not math.isfinite(x):
        return x
    return round(x, -int(math.floor(math.log10(abs(x)))) + (n - 1))


class Steps:
    """Calculation log: every result is recorded with formula, substituted values and unit."""

    def __init__(self):
        self.items = []

    def add(self, sid, name, formula, subst, value, unit, note="", *, inputs=None):
        item = {"id": sid, "name": name, "formula": formula,
                "substitution": subst, "value": r3(value), "unit": unit, "note": note}
        if inputs is not None:
            item["inputs"] = list(inputs)
            item["value_exact"] = value
        self.items.append(item)
        return value


def step_input(name, value, source):
    return {"name": name, "value": value, "source": source}


def source_input(kind, root, path, name=None):
    value = root
    for key in path.split("."):
        value = value[key]
    return step_input(name or path.rsplit(".", 1)[-1], value, f"{kind}:{path}")


def constant_input(name):
    return source_input("const", CALC_CONSTANTS, name)


class Checks:
    """Rule-check log with explicit missing-data and inapplicable states."""

    def __init__(self):
        self.items = []

    def add(self, rid, rule, ok, detail, warn_only=False, ref=""):
        verdict = "NOT_ASSESSED" if ok is None else (
            "PASS" if ok else ("WARN" if warn_only else "FAIL"))
        self.items.append({"id": rid, "rule": rule, "verdict": verdict, "detail": detail, "ref": ref})
        return ok

    def not_applicable(self, rid, rule, reason, ref=""):
        if not reason:
            raise ValueError("N/A requires an explicit reason")
        self.items.append({"id": rid, "rule": rule, "verdict": "N/A", "detail": reason, "ref": ref})

    def summary(self):
        s = {"PASS": 0, "WARN": 0, "FAIL": 0, "NOT_ASSESSED": 0, "N/A": 0}
        for c in self.items:
            s[c["verdict"]] += 1
        return s


def param(part, name, field=("typ", "max", "min")):
    """Read a numeric parameter from a parts_db entry, preferring the given field order."""
    p = (part or {}).get("params", {}).get(name)
    if p is None:
        return None
    if isinstance(p, (int, float)):
        return p
    for f in field:
        if p.get(f) is not None:
            return p[f]
    return None


def part_input(part, name, fields=("typ", "max", "min")):
    """Record the actual typ/max/min column, page and measurement condition."""
    value = param(part, name, fields)
    if value is None:
        return None
    raw = part["params"][name]
    if isinstance(raw, dict):
        field = next(f for f in fields if raw.get(f) is not None)
        record = step_input(name, value, f"part:{part['id']}.{name}.{field}@{raw.get('page') or '未标注'}")
        record["condition"] = raw.get("condition")
    else:
        record = step_input(name, value, f"part:{part['id']}.{name}@未标注")
    return record


def workdir_paths(workdir):
    w = Path(workdir)
    return {
        "requirements": w / "requirements.json",
        "choices": w / "design_choices.json",
        "calc": w / "calc.json",
        "selection": w / "selection.json",
        "losses": w / "losses.json",
        "loop": w / "loop.json",
        "sim": w / "sim.json",
        "check": w / "check.json",
    }
