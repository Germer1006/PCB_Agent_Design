"""Run KiCad's own checks against a generated board without changing it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory


def find_kicad_cli(explicit: str | Path | None = None) -> Path | None:
    """Resolve an explicit override, the PATH, or a Windows/macOS installation."""
    override = explicit or os.environ.get("KICAD_CLI")
    if override:
        path = Path(override).expanduser()
        return path.resolve() if path.is_file() else None
    executable = shutil.which("kicad-cli")
    if executable:
        return Path(executable).resolve()
    if os.name == "nt":
        roots = [Path("D:/KiCad"), Path("C:/KiCad")]
        for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            if os.environ.get(name):
                base = Path(os.environ[name])
                roots.append(base / ("Programs/KiCad" if name == "LOCALAPPDATA" else "KiCad"))
        for root in roots:
            candidates = [root / "bin/kicad-cli.exe", *sorted(root.glob("*/bin/kicad-cli.exe"), reverse=True)]
            for path in candidates:
                if path.is_file():
                    return path.resolve()
    mac = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    return mac if mac.is_file() else None


def _execute(arguments: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        arguments, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def run_drc(
    board: Path,
    output: Path,
    cli: Path,
    *,
    schematic_parity: bool = False,
    refill_zones: bool = False,
    timeout: float = 60,
) -> dict:
    """Return scoped DRC counts and keep KiCad's complete JSON report."""
    board, output, cli = Path(board).resolve(), Path(output).resolve(), Path(cli).resolve()
    result = {
        "status": "failed",
        "violations": None,
        "unconnected": None,
        "errors": None,
        "warnings": None,
        "excluded": None,
        "schematic_parity": None,
        "schematic_parity_requested": schematic_parity,
        "refill_zones": refill_zones,
        "report": None,
        "returncode": None,
    }
    if output.suffix.lower() != ".json" or output == board or output == cli:
        return {**result, "error": "DRC 输出必须是与输入不同的 JSON 文件。"}
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".kicad-drc-", dir=output.parent) as temporary:
            report = Path(temporary) / "report.json"
            args = [
                str(cli), "pcb", "drc", "--format", "json",
                "--severity-all", "--exit-code-violations", "--output", str(report),
            ]
            if schematic_parity:
                args.append("--schematic-parity")
            if refill_zones:
                args.append("--refill-zones")
            process = _execute([*args, str(board)], timeout)
            result["returncode"] = process.returncode
            if report.is_file():
                report.replace(output)
                result["report"] = str(output)
            if process.returncode not in (0, 5) or result["report"] is None:
                details = (process.stderr.strip() or process.stdout.strip())[-1800:]
                return {**result, "error": f"KiCad 没有完成 DRC（退出码 {process.returncode}）。{details}"}
        payload = json.loads(output.read_text(encoding="utf-8-sig"))
    except subprocess.TimeoutExpired:
        return {**result, "error": f"KiCad DRC 超过 {timeout:g} 秒。"}
    except (OSError, ValueError) as error:
        return {**result, "error": str(error)}

    if not isinstance(payload, dict):
        return {**result, "error": "KiCad DRC 报告的根字段不是 JSON 对象。"}
    source = payload.get("source", "")
    if not isinstance(source, str) or source.replace("\\", "/").rsplit("/", 1)[-1] != board.name:
        return {**result, "error": "DRC 报告指向不同的 PCB 文件。"}
    sections = ("violations", "unconnected_items", "schematic_parity")
    for key in sections:
        entries = payload.get(key)
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict)
            or entry.get("severity") not in ("error", "warning")
            or not isinstance(entry.get("type"), str)
            for entry in entries
        ):
            return {**result, "error": f"KiCad DRC 报告的 {key} 字段无效。"}

    all_entries = [entry for key in sections for entry in payload[key]]
    active = [entry for entry in all_entries if not entry.get("excluded", False)]
    counts = Counter(entry["severity"] for entry in active)
    return {
        **result,
        "status": "findings" if all_entries or process.returncode == 5 else "passed",
        "violations": len(payload["violations"]),
        "unconnected": len(payload["unconnected_items"]),
        "schematic_parity": len(payload["schematic_parity"]),
        "errors": counts["error"],
        "warnings": counts["warning"],
        "excluded": len(all_entries) - len(active),
        "types": dict(Counter(entry["type"] for entry in active)),
        "ignored_checks": payload.get("ignored_checks"),
        "included_severities": payload.get("included_severities"),
        "kicad_version": payload.get("kicad_version"),
    }


def render_preview(board: Path, output: Path, cli: Path, *, timeout: float = 30) -> bool:
    """Generate a top-view preview with KiCad; SVG shows copper, PNG shows the board."""
    board, output, cli = Path(board).resolve(), Path(output).resolve(), Path(cli).resolve()
    suffix = output.suffix.lower()
    if suffix not in (".png", ".svg") or output in (board, cli):
        return False
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".kicad-preview-", dir=output.parent) as temporary:
            preview = Path(temporary) / ("board" + suffix)
            if suffix == ".svg":
                args = [
                    "pcb", "export", "svg", "--mode-single", "--fit-page-to-board",
                    "--layers", "F.Cu,B.Cu,F.Silkscreen,Edge.Cuts",
                ]
            else:
                args = [
                    "pcb", "render", "--side", "top", "--width", "1200",
                    "--height", "900", "--background", "opaque",
                ]
            process = _execute([str(cli), *args, "--output", str(preview), str(board)], timeout)
            if process.returncode != 0 or not preview.is_file() or not preview.stat().st_size:
                return False
            preview.replace(output)
            return True
    except (OSError, subprocess.TimeoutExpired):
        return False
