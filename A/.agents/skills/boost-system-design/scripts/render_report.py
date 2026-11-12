"""将报告源文件中的计算占位符替换成当前设计版本的数据。"""

import argparse
import json
import re
from pathlib import Path

from _common import load_json, workdir_paths


TEMPLATE = "system_design_report.template.md"
REPORT = "system_design_report.md"
PLACEHOLDER = re.compile(r"\{\{([^{}\r\n]+)\}\}")
SOURCES = {"calc": "calc", "loss": "losses", "loop": "loop", "choice": "choices", "req": "requirements"}


def render(source, paths):
    data = {}

    def substitute(match):
        kind, separator, path = match[1].strip().partition(":")
        try:
            if not separator or not path:
                raise KeyError(path)
            if kind not in data:
                data[kind] = load_json(paths[SOURCES[kind]])
            root = data[kind]
            if kind in ("calc", "loss", "loop") and "." not in path:
                step = next((step for step in root.get("steps", ()) if step["id"] == path), None)
                if step is not None:
                    return str(step["value"])
            for key in path.split("."):
                root = root[key]
            if not isinstance(root, (str, int, float, bool)):
                raise TypeError(path)
            return root if isinstance(root, str) else json.dumps(root)
        except (KeyError, TypeError) as err:
            raise ValueError(f"报告占位符 {match[0]} 无法解析到标量") from err

    result = PLACEHOLDER.sub(substitute, source)
    if "{{" in result or "}}" in result:
        raise ValueError("报告存在未解析或格式不完整的占位符")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    source = args.source or args.workdir / TEMPLATE
    output = args.workdir / REPORT
    try:
        if source.resolve() == output.resolve():
            raise ValueError("报告源和输出必须是不同文件")
        rendered = render(source.read_text(encoding="utf-8-sig"), workdir_paths(args.workdir))
        output.write_text(rendered, encoding="utf-8")
    except (OSError, ValueError) as err:
        parser.exit(1, f"报告渲染失败：{err}\n")
    print(f"{output.name} 已生成")


if __name__ == "__main__":
    main()
