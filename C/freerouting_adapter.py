"""Optional, explicit Freerouting 2.x repair. Never claim the local router did it."""
from pathlib import Path
import hashlib
import re
import shutil
import subprocess

from kicad_io import Quoted, _parse, _items, _field, emit, native_operation


def configure_dsn(text, protected, signal_width, power_width, clearance):
    if "(unit um)" not in text:
        raise ValueError("Unsupported DSN unit; do not guess clearance conversions")
    # Specctra's parser declaration uses a bare quote, unlike KiCad strings.
    marker = "__DSN_QUOTE_CHARACTER__"
    normalized = text.replace('(string_quote ")', '(string_quote "' + marker + '")')
    tree = _parse(normalized)
    networks = _items(tree, "network")
    if not networks:
        raise ValueError("DSN network missing")
    classes = _items(networks[0], "class")
    if not classes:
        raise ValueError("DSN default class missing")
    for cls in classes:
        cls[:] = [item for index, item in enumerate(cls) if index < 2 or isinstance(item, list) or str(item) not in protected]
    if protected:
        circuit = _items(classes[0], "circuit")
        if not circuit:
            raise ValueError("DSN via rule missing")
        networks[0].append(["class", "pcb_power", *[Quoted(n) for n in sorted(protected)], circuit[0],
                            ["rule", ["width", power_width * 1000], ["clearance", clearance * 1000]]])
    # KiCad's exported SMD-pad exception must not silently reduce the clearance.
    def rewrite(node):
        if not isinstance(node, list):
            return
        if node and node[0] == "clearance" and _items(node, "type"):
            node[1] = str(clearance * 1000)
        for child in node:
            rewrite(child)
    rewrite(tree)
    return emit(tree).replace('(string_quote "' + marker + '")', '(string_quote ")') + "\n"


def reroute(board, design, work_dir, *, jar, signal_width, power_width, clearance, power_nets,
            timeout=180, java=None, ground_nets=None):
    board, work_dir, jar = Path(board).resolve(), Path(work_dir).resolve(), Path(jar).resolve(strict=True)
    java = java or shutil.which("java")
    if java is None:
        raise RuntimeError("Freerouting needs Java; no fallback success is assumed")
    work_dir.mkdir(parents=True, exist_ok=True)
    tree = _parse(board.read_text(encoding="utf-8"))
    names = {str(net[1]): str(net[2]) for net in _items(tree, "net") if len(net) >= 3}
    if ground_nets is None:
        ground_nets = {n for n in power_nets if n.rsplit("/", 1)[-1].upper() in {"GND", "PGND", "AGND"}}
    protected = set(power_nets) - set(ground_nets)
    # Use text filtering instead of unsafe SWIG Remove iteration on KiCad 9.
    kept = []
    for item in tree:
        if isinstance(item, list) and item:
            if item[0] in {"segment", "via"}:
                key = str(_field(item, "net"))
                if names.get(key, key) not in protected:
                    continue
            if item[0] == "zone":
                raise ValueError("Rerouting an existing copper-pour design requires explicit zone handling")
        kept.append(item)
    base, dsn, session = work_dir / "power_base.kicad_pcb", work_dir / "repair.dsn", work_dir / "repair.ses"
    base.write_text(emit(kept) + "\n", encoding="utf-8")
    native_operation(design, "dsn", {"board": str(base), "output": str(dsn),
                     "width": signal_width, "clearance": clearance}, work_dir)
    text = dsn.read_text(encoding="utf-8")
    text = configure_dsn(text, protected, signal_width, power_width, clearance)
    lines = [line.replace("(type route)", "(type protect)") if "(wire (path" in line else line
             for line in text.splitlines()]
    dsn.write_text("\n".join(lines) + "\n", encoding="utf-8")
    command = [str(java), "-Djava.awt.headless=true", "-Xmx2g", "-jar", str(jar),
               "-de", str(dsn), "-do", str(session), "-mp", "60", "-mt", "0", "-da",
               "--gui.enabled=false", "--api_server.enabled=false", "--usage_and_diagnostic_data.disable_analytics=true"]
    with (work_dir / "freerouting.log").open("wb") as log:
        process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
    if process.returncode or not session.is_file() or not session.stat().st_size:
        raise RuntimeError("Freerouting failed or produced no session; see freerouting.log")
    repaired = work_dir / "repaired.kicad_pcb"
    native_operation(design, "ses", {"board": str(base), "session": str(session), "output": str(repaired)}, work_dir)
    # Native DRC/readback still decide whether this attempt improved the board.
    return repaired, {"engine": "freerouting", "jar_sha256": hashlib.sha256(jar.read_bytes()).hexdigest(),
                      "command": command, "protected_power_nets": sorted(protected),
                      "ground_neckdown_width_mm": signal_width,
                      "warning": "Signal/GND copper was rebuilt; review/add ground planes and current-carrying return paths."}
