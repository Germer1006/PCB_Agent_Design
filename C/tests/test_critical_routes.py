"""Regressions for missing driver copper and off-pad Kelvin takeoffs."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from critical_routes import check_routes, plan_pairs
from router import route_board


def board_text(pads, segments, vias=(), *, old=False):
    names = {p["net"]: i for i, p in enumerate(pads, 1)}

    def net(name, *, pad=False):
        if not old:
            return f'(net "{name}")'
        return f'(net {names[name]} "{name}")' if pad else f"(net {names[name]})"

    items = [f'(net {code} "{name}")' for name, code in names.items()] if old else []
    for p in pads:
        items.append(
            f'(footprint "Test" (at {p["x"]} {p["y"]}) (layer "F.Cu") (property "Reference" "{p["ref"]}") '
            f'(pad "{p["number"]}" smd rect (at 0 0) (size {p["w"]} {p["h"]}) (layers "F.Cu") {net(p["net"], pad=True)}))'
        )
    for s in segments:
        layer = ("F.Cu", "B.Cu")[s["layer"]]
        start, end = (f"{x} {y}" for x, y in (s["start"], s["end"]))
        items.append(f'(segment (start {start}) (end {end}) (width {s["width"]}) (layer "{layer}") {net(s["net"])})')
    for via in vias:
        items.append(f'(via (at {via["x"]} {via["y"]}) (layers "F.Cu" "B.Cu") {net(via["net"])})')
    return "(kicad_pcb " + "\n".join(items) + ")"


def pad(ref, x, y, net="/VBOOST"):
    return {"ref": ref, "number": "1", "net": net, "x": x, "y": y, "w": 1.0, "h": 1.0,
            "layers": [0], "shape": "rect"}


def track(start, end, *, net="/VBOOST", layer=0):
    return {"net": net, "layer": layer, "start": start, "end": end, "width": .25}


class CriticalRouteTests(unittest.TestCase):
    def checks(self, pads, segments, contract, vias=(), *, old=False):
        with TemporaryDirectory() as tmp:
            board = Path(tmp) / "fixture.kicad_pcb"
            board.write_text(board_text(pads, segments, vias, old=old), encoding="utf-8")
            return {c["id"]: c for c in check_routes(board, contract)}

    def test_kelvin_branch_at_interior_junction_is_not_accepted(self):
        pads = [pad("SHUNT", 4, 5), pad("SENSE", 8, 5), pad("POWER", 6, 9)]
        c = {"pad_pairs": [{"id": "sense", "kind": "kelvin", "a": ["SHUNT", "1"], "b": ["SENSE", "1"]}]}
        common = [track((4, 5), (8, 5))]
        for old in (True, False):
            with self.subTest(numeric_net_format=old):
                shared = self.checks(pads, [*common, track((6, 5), (6, 9))], c, old=old)["copper:sense"]
                self.assertEqual(shared["status"], "failed", shared)
                separate = self.checks(pads, [*common, track((4, 5), (6, 9))], c, old=old)["copper:sense"]
                self.assertEqual(separate["status"], "passed", separate)

    def test_driver_audit_reports_missing_trace_and_layer_changes(self):
        net = "/DRV_SOURCE"
        pads = [pad("U", 2, 4, net), pad("R", 10, 4, net)]
        c = {"groups": [{"id": "gate_driver_cluster", "refs": ["U", "R"]}]}
        self.assertEqual(self.checks(pads, [], c)["copper:/DRV_SOURCE"]["status"], "failed")
        segments = [track((2, 4), (4, 4), net=net), track((4, 4), (8, 4), net=net, layer=1),
                    track((8, 4), (10, 4), net=net)]
        vias = [{"net": net, "x": x, "y": 4} for x in (4, 8)]
        finding = self.checks(pads, segments, c, vias)["copper:/DRV_SOURCE"]
        self.assertEqual((finding["status"], finding["track_length_mm"], finding["layer_changes"]), ("passed", 8, 2))

    def test_local_kelvin_copper_is_reserved_before_the_power_tree(self):
        pads = [pad("SHUNT", 4, 5), pad("SENSE", 8, 5), pad("POWER", 10, 5)]
        c = {"pad_pairs": [{"id": "sense", "kind": "kelvin", "a": ["SHUNT", "1"], "b": ["SENSE", "1"]}]}
        result = route_board(pads, 14, 10, widths={"/VBOOST": .25}, priority_pairs=plan_pairs(pads, c))
        self.assertEqual(result["priority_pairs"][0]["status"], "routed")
        self.assertEqual(result["unrouted"], [])
        finding = self.checks(pads, result["segments"], c, result["vias"])["copper:sense"]
        self.assertEqual(finding["status"], "passed", finding)
