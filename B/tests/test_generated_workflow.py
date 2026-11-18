"""Cross-topology regressions through the normal ABC entry points."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "A"))
sys.path.insert(0, str(ROOT / "B"))

from design_circuit import design_divider, design_rc
from generate_schematic import render


def example(name):
    return json.loads((ROOT / "A" / "inputs" / (name + ".example.json")).read_text(encoding="utf-8"))


class CircuitDesignTests(unittest.TestCase):
    def test_rc_channel_count_and_nominal_check(self):
        requirements = example("rc_lowpass")
        requirements["channels"] = 2
        spec = design_rc(requirements)
        self.assertEqual(len(spec["schematic_intent"]["components"]), 6)
        self.assertFalse(spec["formal_handoff"])
        self.assertAlmostEqual(spec["calculation"]["cutoff_hz"], 33862.75385, places=4)
        requirements["capacitor_options_nf"] = [10]
        with self.assertRaisesRegex(ValueError, "No capacitor"):
            design_rc(requirements)

    def test_divider_nominal_ceiling(self):
        requirements = example("voltage_divider")
        spec = design_divider(requirements)
        self.assertEqual(spec["calculation"]["output_at_max_input_v"], 3)
        self.assertFalse(spec["formal_handoff"])
        requirements["resistor_options_ohm"] = [10000]
        with self.assertRaisesRegex(ValueError, "No resistor pair"):
            design_divider(requirements)


CLI = shutil.which("kicad-cli")
SYMBOLS = Path(CLI).resolve().parent.parent / "share" / "kicad" / "symbols" if CLI else None


@unittest.skipUnless(CLI and (SYMBOLS / "Device.kicad_sym").is_file(), "Native KiCad symbols/CLI required")
class NativeGeneratedWorkflow(unittest.TestCase):
    def test_render_rejects_unaccounted_pins(self):
        circuit = design_divider(example("voltage_divider"))["schematic_intent"]
        circuit["nets"][2]["pins"].pop()
        with self.assertRaisesRegex(ValueError, "Declare every pin"):
            render(circuit, "circuit", SYMBOLS)

    def test_two_non_boost_designs_through_native_kicad(self):
        for case in ("rc_lowpass", "voltage_divider"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as work:
                path = Path(work)
                spec, handoff, runs = path / "spec.json", path / "handoff", path / "runs"

                def run(file, *args):
                    cmd = [sys.executable, "-B", str(ROOT / file), *map(str, args)]
                    return subprocess.run(cmd, capture_output=True, text=True, timeout=90)

                a = run("A/design_circuit.py", ROOT / "A" / "inputs" / (case + ".example.json"), "--output", spec)
                self.assertEqual(a.returncode, 0, a.stderr)
                b = run("B/generate_schematic.py", spec, "--output", handoff)
                self.assertEqual(b.returncode, 0, b.stderr)
                self.assertEqual(json.loads(b.stdout)["ERC"], "passed", b.stdout)
                c = run("C/auto_pcb.py", handoff / "project_input.json", "--output", runs,
                        "--size", "36", "26", "--attempts", "2", "--router-timeout", "15", "--no-preview",
                        "--ground-planes", "--target", "draft")
                self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
                output = next(runs.iterdir())
                status = json.loads((output / "execution_status.json").read_text(encoding="utf-8"))
                self.assertEqual(status["drc"]["unconnected"], 0)
                self.assertTrue(status["drc"]["schematic_parity_requested"])
                self.assertEqual(status["drc"]["schematic_parity"], 0)
                self.assertFalse(status["prototype_ready"])
                route = json.loads((output / "布局与布线.json").read_text(encoding="utf-8"))
                self.assertEqual(route["routing_policy"]["ground_plane_net"], "/RET_ANALOG")
                self.assertEqual(route["routing_policy"]["widths_mm"]["/RET_ANALOG"], .45)
                with zipfile.ZipFile(output / "delivery.zip") as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertIn("project/symbols/Device.kicad_sym", archive.namelist())
                    self.assertEqual(json.loads(archive.read("roundtrip_check.json"))["status"], "passed")
