"""核对同一设计版本的规格、BOM、固件头文件和报告。"""

import argparse
import csv
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from tempfile import TemporaryDirectory

from _common import KNOWLEDGE_DIR, load_json, workdir_paths
from export_handoff import handoff_inputs, prepare_handoff, write_bom, write_tables
from handoff_header import write_header
from render_report import REPORT, TEMPLATE, render


def compare_json(actual, expected, path):
    """Yield paths to differences instead of printing a whole document."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(expected.keys() | actual.keys()):
            child = f"{path}.{key}"
            if key not in actual:
                yield f"{child} 缺失"
            elif key not in expected:
                yield f"{child} 为多余字段"
            else:
                yield from compare_json(actual[key], expected[key], child)
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            yield f"{path} 长度应为 {len(expected)}，实际 {len(actual)}"
        for index, (value, reference) in enumerate(zip(actual, expected)):
            yield from compare_json(value, reference, f"{path}[{index}]")
    elif actual != expected or isinstance(actual, bool) != isinstance(expected, bool):
        yield f"{path} 应为 {expected!r}，实际 {actual!r}"


def read_bom(path):
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        rows = {}
        for row in reader:
            designator = row.get("位号")
            if not designator or designator in rows or None in row or None in row.values():
                raise ValueError(f"{path.name}:{reader.line_num} 位号重复、缺失或列数错误")
            rows[designator] = row
        return reader.fieldnames, rows


def compare_text(name, actual, expected):
    actual_lines, expected_lines = actual.splitlines(), expected.splitlines()
    for op, start, end, ref_start, ref_end in SequenceMatcher(
            None, actual_lines, expected_lines, autojunk=False).get_opcodes():
        if op != "equal":
            excerpt = " / ".join(expected_lines[ref_start:ref_end][:2])[:180] or "（无对应行）"
            found = " / ".join(actual_lines[start:end][:2])[:180] or "（缺失）"
            yield f"{name}:{start + 1} 应为 {excerpt}，实际 {found}"


def check(workdir, db):
    J = handoff_inputs(workdir)
    revision = J["choices"]["revision"]
    paths = workdir_paths(workdir)
    mismatched = [f"{paths[key].name}.revision 应为 {revision!r}，实际 {J[key].get('revision')!r}"
                  for key in ("calc", "selection", "losses", "loop", "check", "sim")
                  if J[key] is not None and J[key].get("revision") != revision]
    if mismatched:
        return mismatched
    expected = prepare_handoff(J, db)
    actual = load_json(workdir / "design_spec.json")
    stamp = actual.get("date", "")
    if isinstance(stamp, str):
        try:
            expected["date"] = date.fromisoformat(stamp).isoformat()
        except ValueError:
            pass
    errors = list(compare_json(actual, expected, "design_spec.json"))

    for name in ("loop_validation.json", "ledger.json"):
        path = workdir / name
        if path.exists() and load_json(path).get("revision") != expected["revision"]:
            errors.append(f"{name}.revision 应为 {expected['revision']!r}")

    with TemporaryDirectory(prefix="boost-handoff-") as tmp:
        ref = Path(tmp)
        bom = "bom_module1.csv"
        write_bom(ref / bom, expected)
        expected_head, expected_rows = read_bom(ref / bom)
        actual_head, actual_rows = read_bom(workdir / bom)
        errors.extend(compare_json(actual_head, expected_head, bom + ".列名"))
        errors.extend(compare_json(actual_rows, expected_rows, bom))

        write_header(ref / "boost_params.h", J["calc"], J["loop"], J["choices"])
        write_tables(ref / "report_tables.md", expected, J["calc"], J["selection"], J["losses"], J["loop"], J["check"])
        for name in ("boost_params.h", "report_tables.md"):
            errors.extend(compare_text(name, (workdir / name).read_text(encoding="utf-8-sig"),
                                       (ref / name).read_text(encoding="utf-8")))

    template = workdir / TEMPLATE
    report = workdir / REPORT
    if template.exists() or report.exists():
        body = report.read_text(encoding="utf-8-sig")
        if "{{" in body or "}}" in body:
            errors.append(f"{REPORT} 含未替换的占位符")
        if template.exists():
            rendered = render(template.read_text(encoding="utf-8-sig"), paths)
            errors.extend(compare_text(REPORT, body, rendered))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--db", type=Path, default=KNOWLEDGE_DIR / "parts_db.json")
    args = parser.parse_args()
    try:
        errors = check(args.workdir, load_json(args.db))
    except (OSError, KeyError, TypeError, ValueError) as err:
        parser.exit(1, f"交接一致性校验失败：{err}\n")
    if errors:
        parser.exit(1, "交接一致性校验失败：\n" + "\n".join(errors) + "\n")
    print("交接一致性通过：design_spec.json / bom_module1.csv / boost_params.h / report_tables.md")


if __name__ == "__main__":
    main()
