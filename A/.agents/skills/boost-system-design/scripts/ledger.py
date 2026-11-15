"""汇总并校验计算依据，生成 ledger.json / ledger.md 或查询变更影响。"""

import argparse
from pathlib import Path

from _common import CALC_CONSTANTS, KNOWLEDGE_DIR, load_json, save_json, workdir_paths
from export_handoff import handoff_inputs, run
from trace_graph import build_graph, stale_records


def resolve_source(item, roots, previous, parts):
    """Resolve a reference against the same inputs used by the producing script."""
    source = item["source"]
    kind, sep, path = source.partition(":")
    if not sep or not path:
        raise ValueError(f"无效来源 {source!r}")
    try:
        if kind == "step":
            return previous[path]["value_exact"]
        if kind == "part":
            field_path, at, page = path.rpartition("@")
            pid, _, field = field_path.partition(".")
            if not at:
                raise KeyError("page")
            part = parts[pid]
            selection = roots["selection"]
            if selection and pid not in {category["selected"] for category in selection["categories"].values()}:
                raise ValueError(f"{pid} 已不在本次入选器件中")
            param_name, _, column = field.partition(".")
            if column:
                entry = part["params"][param_name]
                if (entry.get("page") or "未标注") != page or entry.get("condition") != item.get("condition"):
                    raise ValueError(f"{source!r} 的页码或测试条件已变化")
                return entry[column]
            if page != "未标注":
                raise KeyError("page")
            return part["params"][field] if field in part.get("params", {}) else part[field]
        if kind == "selection":
            role, _, field = path.partition(".")
            category = roots["selection"]["categories"].get(role, {})
            if field == "qty":
                return category.get("selected_qty") or (1 if category.get("selected") else 0)
            if field == "selected":
                return category.get("selected")
            raise KeyError(field)
        value = roots[kind]
        for name in path.split("."):
            value = value[name]
        return value
    except (KeyError, TypeError) as err:
        raise ValueError(f"无法解析来源 {source!r}") from err


def build_ledger(req, choices, calc, *, losses=None, loop=None, selection=None, db=None, check=None):
    artifacts = {"calc": calc, "loss": losses, "loop": loop, "selection": selection, "check": check}
    revision = choices["revision"]
    for stage, data in artifacts.items():
        if data is not None and data.get("revision") != revision:
            raise ValueError(f"{stage} 与设计决策的版本不一致")
    roots = {"req": req, "choice": choices, "const": CALC_CONSTANTS, **artifacts}
    parts = {part["id"]: part for part in (db or {}).get("parts", [])}
    seen = {}
    traced = []
    for stage in ("calc", "loss", "loop"):
        data = artifacts[stage]
        if data is None:
            continue
        if "steps" not in data:
            raise ValueError(f"{stage} 缺少计算记录，请重跑对应步骤")
        for step in data["steps"]:
            sid = step["id"]
            if sid in seen:
                raise ValueError(f"重复的计算步 {sid}")
            if not step.get("inputs") or "value_exact" not in step:
                raise ValueError(f"{sid} 缺少来源记录，请重跑 {stage} 对应步骤")
            for item in step["inputs"]:
                source = item["source"]
                try:
                    expected = resolve_source(item, roots, seen, parts)
                except ValueError as err:
                    raise ValueError(f"{sid} / {item['name']}：{err}") from err
                if item["value"] != expected:
                    raise ValueError(f"{sid} / {item['name']}：记录值与 {source} 不一致")
            traced.append({**step, "stage": stage})
            seen[sid] = step
    if not traced:
        raise ValueError("calc.json 中没有来源记录，请重跑 calc_boost.py")
    return {
        "schema": "boost-calc-ledger/2",
        "revision": revision,
        "coverage": {"traced": len(traced), "total": len(seen)},
        "steps": traced,
        "dependencies": build_graph(traced, selection or {}, check or {}),
    }


def write_markdown(path, ledger, traceability=()):
    coverage = ledger["coverage"]
    lines = [
        "# 计算依据账本", "",
        f"设计版本 `{ledger['revision']}`。当前记录 {coverage['traced']}/{coverage['total']} 个计算步。", "",
    ]
    if traceability:
        lines.extend(["| 需求 | 计算步 | 器件 | 规则 | 测试 |", "|---|---|---|---|---|"])
        for row in traceability:
            cells = [row["req"], *(", ".join(row[k]) or "—" for k in ("calculation_steps", "parts", "checks", "tests"))]
            lines.append("| " + " | ".join(cells) + " |")
        lines.extend(["", "## 各计算步依据", ""])
    lines.extend(["| 计算步 | 公式与代入 | 结果 | 输入来源 |", "|---|---|---|---|"])
    for step in ledger["steps"]:
        inputs = "；".join(f"{item['name']} = {item['value']}（`{item['source']}`）"
                          + (f"，条件 {item['condition']}" if item.get("condition") else "")
                          for item in step["inputs"])
        cells = (f"`{step['id']}` {step['name']}", f"{step['formula']}<br>{step['substitution']}",
                 f"{step['value']} {step['unit']}", inputs)
        lines.append("| " + " | ".join(cell.replace("|", r"\|").replace("\n", "<br>") for cell in cells) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--db", type=Path, default=KNOWLEDGE_DIR / "parts_db.json")
    parser.add_argument("--stale", metavar="PART_ID_OR_CHOICE_KEY")
    args = parser.parse_args()
    paths = workdir_paths(args.workdir)
    traceability = ()
    try:
        if args.stale and (args.workdir / "ledger.json").exists():
            # Query the last saved dependency graph even if the choices or parts have since changed.
            ledger = load_json(args.workdir / "ledger.json")
        else:
            db = load_json(args.db)
            optional = {key: load_json(paths[key]) for key in ("losses", "loop", "selection", "check")
                        if paths[key].exists()}
            ledger = build_ledger(*(load_json(paths[key]) for key in ("requirements", "choices", "calc")),
                                  **optional, db=db)
        if args.stale:
            result = stale_records(ledger["dependencies"], args.stale)
            print(f"设计 {ledger['revision']} · {args.stale}")
            for key, title in (("steps", "计算步"), ("rules", "规则"), ("handoff", "交接字段")):
                print(f"{title}（{len(result[key])}）")
                for field in result[key]:
                    print(f"  {field}")
            return
        if all(key in optional for key in ("losses", "loop", "selection", "check")):
            J = handoff_inputs(args.workdir)
            spec, _, _ = run(J["requirements"], J["choices"], J["calc"], J["selection"],
                             J["losses"], J["loop"], J["check"], db)
            traceability = spec["traceability"]
    except (OSError, KeyError, ValueError) as err:
        parser.exit(1, f"账本生成失败：{err}\n")
    save_json(args.workdir / "ledger.json", ledger)
    write_markdown(args.workdir / "ledger.md", ledger, traceability)
    print(f"ledger.json / ledger.md 已生成，覆盖 {ledger['coverage']['traced']}/{ledger['coverage']['total']} 步")


if __name__ == "__main__":
    main()
