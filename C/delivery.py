"""Check the exact project after ZIP extraction, then publish a coherent bundle."""
from pathlib import Path
import json
import shutil
from tempfile import TemporaryDirectory
import zipfile

from kicad_io import native_operation
from release_check import evaluate, read_expected, check_readback, snapshot
from verify import run_drc


def _refresh_release_outputs(directory, gate):
    """Keep human-readable and machine-readable results on the same final gate."""
    directory = Path(directory)
    (directory / "release_status.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name in ("execution_status.json", "布局与布线.json"):
        path = directory / name
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            data["prototype_ready"] = gate["prototype_ready"]
            data["production_ready"] = False
            if name == "execution_status.json":
                if gate["prototype_ready"]:
                    data["status"] = "prototype_ready"
                elif data.get("status") == "prototype_ready":
                    data["status"] = "review_or_rework_required"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = directory / "设计报告.md"
    if report.is_file():
        text = report.read_text(encoding="utf-8")
        start, end = "## 当前放行阻塞项", "## 交付复查"
        if start in text and end in text:
            before, rest = text.split(start, 1)
            _, after = rest.split(end, 1)
            rows = [f"- `{b['id']}`：{b['detail']}" for b in gate["blockers"]]
            status = f"- 当前审核状态：{gate['status']}；production_ready=false"
            report.write_text(before + start + "\n\n" + status + "\n" +
                              "\n".join(rows) + "\n\n" + end + after, encoding="utf-8")


def package_delivery(directory, *, cli, design):
    directory = Path(directory).resolve()
    proof = json.loads((directory / "verification.json").read_text(encoding="utf-8"))
    project = directory / proof["project"]
    names = ("verification.json", "DRC.json", "layout_check.json", "release_status.json",
             "readback.json", "execution_status.json", "布局与布线.json", "设计报告.md", "release_review.json")
    with TemporaryDirectory(prefix=".delivery-", dir=directory) as temporary:
        temporary = Path(temporary)
        stage = temporary / "stage"
        stage.mkdir()
        shutil.copytree(project, stage / proof["project"])
        for name in names:
            if (directory / name).is_file():
                shutil.copyfile(directory / name, stage / name)
        if (directory / "preview").is_dir():
            shutil.copytree(directory / "preview", stage / "preview")
        initial = temporary / "candidate.zip"
        def pack(destination):
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(stage.rglob("*")):
                    if path.is_file() and not path.name.startswith("~") and path.suffix not in {".lck", ".kicad_prl"}:
                        archive.write(path, path.relative_to(stage))
        pack(initial)
        extracted = temporary / "extracted"
        with zipfile.ZipFile(initial) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("ZIP integrity check failed")
            archive.extractall(extracted)
        extracted_project = extracted / proof["project"]
        if snapshot(extracted_project) != proof["artifacts_sha256"]:
            raise RuntimeError("Packaged project differs from the verified project")
        board = extracted_project / proof["board"]
        drc = run_drc(board, temporary / "roundtrip_DRC.json", cli,
                      schematic_parity=proof["drc"].get("schematic_parity_requested", False))
        audit = native_operation(design, "audit", {"board": str(board)}, temporary)
        readback = check_readback(*read_expected(extracted_project), audit)
        # Partial drafts can be delivered, but extraction may not improve/fake status.
        stable = all(drc.get(key) == proof["drc"].get(key) for key in ("status", "errors", "warnings", "unconnected", "schematic_parity"))
        if drc["status"] == "failed" or not stable or readback["status"] != "passed":
            raise RuntimeError("Extracted-project DRC/readback disagrees; no final delivery published")
        result = {"status": "passed", "same_project_hashes": True,
                  "artifacts_sha256": proof["artifacts_sha256"],
                  "native_drc": {k: drc[k] for k in ("status", "errors", "warnings", "unconnected", "schematic_parity")},
                  "readback": readback,
                  "gui_reopen": "Manual review required; CLI roundtrip is not GUI validation"}
        (extracted / "roundtrip_check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        gate = evaluate(extracted)
        result["prototype_ready"] = gate["prototype_ready"]
        _refresh_release_outputs(stage, gate)
        (stage / "roundtrip_check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        final = temporary / "delivery.zip"
        pack(final)
        final.replace(directory / "delivery.zip")
        shutil.copyfile(stage / "roundtrip_check.json", directory / "roundtrip_check.json")
        _refresh_release_outputs(directory, gate)
    return result
