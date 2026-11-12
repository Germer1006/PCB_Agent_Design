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

    def add(self, sid, name, formula, subst, value, unit, note=""):
        self.items.append({"id": sid, "name": name, "formula": formula,
                           "substitution": subst, "value": r3(value), "unit": unit, "note": note})
        return value


class Checks:
    """Rule-check log with PASS / WARN / FAIL verdicts."""

    def __init__(self):
        self.items = []

    def add(self, rid, rule, ok, detail, warn_only=False, ref=""):
        verdict = "PASS" if ok else ("WARN" if warn_only else "FAIL")
        self.items.append({"id": rid, "rule": rule, "verdict": verdict, "detail": detail, "ref": ref})
        return ok

    def summary(self):
        s = {"PASS": 0, "WARN": 0, "FAIL": 0}
        for c in self.items:
            s[c["verdict"]] += 1
        return s


def param(part, name, field=("typ", "max", "min")):
    """Read a numeric parameter from a parts_db entry, preferring the given field order."""
    p = part.get("params", {}).get(name)
    if p is None:
        return None
    if isinstance(p, (int, float)):
        return p
    for f in field:
        if p.get(f) is not None:
            return p[f]
    return None


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
