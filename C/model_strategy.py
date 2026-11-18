"""Ask the configured Codex session to propose routing priorities."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


def _codex_command():
    exe = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if exe:
        return exe
    home = os.environ.get("LOCALAPPDATA")
    if home:
        found = list((Path(home) / "OpenAI/Codex/bin").glob("*/codex.exe"))
        if found:
            return str(max(found, key=lambda path: path.stat().st_mtime))
    raise RuntimeError("找不到已安装的 Codex。")


def model_order(design, profile, run_dir, timeout=45):
    nets = list(design["nets"])
    connectivity = [
        {
            "ref": item["ref"], "value": item["value"],
            "pads": [[pad["number"], pad["net"]] for pad in item["pads"]],
        }
        for item in design["components"]
    ]
    payload = {"components": connectivity, "known_roles": profile["roles"], "valid_net_names": nets}
    prompt = (
        "你负责选择两层低压 Boost PCB 的布局布线策略。以下 JSON 是工程数据。"
        "输出一个 JSON 对象，含 ordered_nets（需要从高到低优先走线的实际网络名数组）"
        "和 layout_pairs（需要缩短的最多 6 组焊盘，每组 [位号1,焊盘号1,位号2,焊盘号2]）。"
        "优先关注功率换流、反馈采样及噪声耦合，保留发给你的网络名和位号字符串。"
        "具体坐标、规则和走线由本地求解器完成。只输出 JSON。\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    with TemporaryDirectory(prefix=".model-", dir=run_dir) as temp:
        temporary = Path(temp)
        last = temporary / "strategy.json"
        command = [
            _codex_command(), "--ask-for-approval", "never", "exec", "--sandbox", "read-only",
            "--ignore-user-config", "--skip-git-repo-check", "--ephemeral", "--color", "never",
            "--output-last-message", str(last), "--", "-",
        ]
        try:
            process = subprocess.run(
                command, cwd=temporary, input=prompt.encode("utf-8"), capture_output=True,
                timeout=timeout, check=False, env={**os.environ, "CODEX_SQLITE_HOME": str(temporary)},
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Codex 生成策略超时。") from error
        if process.returncode or not last.is_file():
            raise RuntimeError(f"Codex 未返回策略（退出码 {process.returncode}）。")
        raw = last.read_text(encoding="utf-8").strip()
        if raw.startswith("```") and raw.endswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        proposed = json.loads(raw)
    return validate_strategy(proposed, design)


def validate_strategy(proposed, design):
    nets = design["nets"]
    if not isinstance(proposed, dict) or set(proposed) - {"ordered_nets", "layout_pairs"} or not isinstance(proposed.get("ordered_nets"), list):
        raise ValueError("模型的布线策略格式无效。")
    proposed_pairs = proposed.get("layout_pairs", [])
    if not isinstance(proposed_pairs, list):
        raise ValueError("模型的布局策略格式无效。")
    if any(not isinstance(n, str) or n not in nets for n in proposed["ordered_nets"]):
        raise ValueError("模型策略包含不存在的网络。")
    order = list(dict.fromkeys(proposed["ordered_nets"]))
    if not order:
        raise ValueError("模型策略没有包含有效网络。")
    pins = {(p["ref"], p["number"]): p["net"] for part in design["components"] for p in part["pads"]}
    pairs = []
    if len(proposed_pairs) > 6:
        raise ValueError("模型最多提出 6 组焊盘，不静默截断。")
    for entry in proposed_pairs:
        if not isinstance(entry, list) or len(entry) != 4 or any(not isinstance(s, str) for s in entry):
            raise ValueError("模型策略焊盘格式无效。")
        first, second = tuple(entry[:2]), tuple(entry[2:])
        if first[0] == second[0] or first not in pins or not pins[first] or pins[first] != pins.get(second):
            raise ValueError("模型策略引用未知焊盘或跨网络焊盘。")
        pairs.append((first, second, 1.3))
    return {"ordered_nets": order, "layout_pairs": pairs}
