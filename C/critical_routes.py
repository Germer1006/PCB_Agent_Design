"""Plan and audit critical copper on two-layer KiCad boards."""

from argparse import ArgumentParser
from collections import defaultdict, deque
from heapq import heappop, heappush
import json
from math import cos, dist, radians, sin
from pathlib import Path

from constraints import read_constraints
from kicad_io import _field, _items, _parse
from router import _pad_distance, _segment_distance

KINDS = ("kelvin", "gate_drive", "power_loop", "decoupling")
DRIVER_OUTPUTS = {"DRV_SOURCE", "DRV_SINK"}
EPSILON = 1e-5  # Tolerance for joining track centreline coordinates.
PAD_SHAPES = {"rect", "roundrect", "oval", "circle"}


def _label(net):
    return net.rsplit("/", 1)[-1].upper()


def plan_pairs(pads, constraints):
    """Use actual pins, including the named Boost groups in older B handoffs."""
    by_pin = defaultdict(list)
    by_net = defaultdict(list)
    for pad in pads:
        pin = pad["ref"], pad["number"]
        by_pin[pin].append(pad)
        if pad["net"] and pin not in by_net[pad["net"]]:
            by_net[pad["net"]].append(pin)
    contracts = constraints or {}
    groups = {g["id"]: g["refs"] for g in contracts.get("groups", [])}
    gate = groups.get("gate_driver_cluster", [])
    power = groups.get("boost_power_cluster", [])
    switching_refs = {pin[0] for net, pins in by_net.items() if _label(net) in {"SW", "LX"} for pin in pins}
    pairs = []
    seen = set()

    def add(a, b, kind, identity):
        signature = frozenset((a, b))
        if signature in seen:
            return
        a_nets, b_nets = [{pad["net"] for pad in by_pin[pin]} for pin in (a, b)]
        if len(a_nets) != 1 or a_nets != b_nets or "" in a_nets:
            return  # Cross-net placement constraints do not describe a copper connection.
        seen.add(signature)
        pairs.append({"id": identity, "kind": kind, "a": a, "b": b, "net": next(iter(a_nets))})

    for item in sorted(contracts.get("pad_pairs", []), key=lambda p: p["kind"] != "kelvin"):
        if item["kind"] in KINDS:
            add(tuple(item["a"]), tuple(item["b"]), item["kind"], item["id"])
    for net, pins in sorted(by_net.items()):
        label = _label(net)
        if label in DRIVER_OUTPUTS and len(pins) == 2 and all(pin[0] in gate for pin in pins):
            add(*pins, "gate_drive", f"{net}")
        elif label == "GATE":
            local = [pin for pin in pins if pin[0] in gate]
            anchors = [pin for pin in local if pin[0] in switching_refs]
            if len(anchors) == 1:
                for pin in local:
                    if pin != anchors[0]:
                        add(anchors[0], pin, "gate_drive", f"{net}:{pin[0]}.{pin[1]}")
        if label in {"SW", "LX", "VBOOST", "VOUT", "GND", "PGND", "VIN"}:
            local = [pin for ref in power for pin in pins if pin[0] == ref]
            if len(local) == 2 or (label in {"SW", "LX"} and len(local) == 3):
                for pin in local[1:]:
                    add(local[0], pin, "power_loop", f"{net}:{pin[0]}.{pin[1]}")
    return sorted(pairs, key=lambda pair: KINDS.index(pair["kind"]))


def _read_copper(path):
    board = _parse(Path(path).read_text(encoding="utf-8-sig"))
    if board[0] != "kicad_pcb":
        raise ValueError("Expected a KiCad PCB")
    names = {row[1]: row[2] for row in _items(board, "net") if len(row) == 3}

    def net(item):
        key = _field(item, "net")
        return names.get(key, key)

    def xy(item, tag):
        return tuple(float(v) for v in _items(item, tag)[0][1:3])

    def angle(item):
        return radians(float(_items(item, "at")[0][3])) if len(_items(item, "at")[0]) > 3 else 0.0

    def layers(item):
        row = set(_items(item, "layers")[0][1:])
        return [layer for layer in ("F.Cu", "B.Cu") if layer in row or "*.Cu" in row or "F&B.Cu" in row]

    pads = []
    for footprint in _items(board, "footprint") + _items(board, "module"):
        ref = next((p[2] for p in _items(footprint, "property") if p[1] == "Reference"), None)
        if ref is None:
            ref = next((p[2] for p in _items(footprint, "fp_text") if p[1] == "reference"), None)
        if not ref:
            raise ValueError("PCB footprint has no reference")
        px, py = xy(footprint, "at")
        fa = angle(footprint)
        for item in _items(footprint, "pad"):
            if not net(item) or item[2] == "np_thru_hole":
                continue
            x, y = xy(item, "at")
            w, h = xy(item, "size")
            pads.append({"ref": ref, "number": item[1], "net": net(item), "shape": item[3],
                         "x": px + x * cos(fa) + y * sin(fa), "y": py - x * sin(fa) + y * cos(fa),
                         "w": w, "h": h, "angle": angle(item), "layers": layers(item),
                         "radius": min(w, h) * float(_field(item, "roundrect_rratio", 0)),
                         "geometry_known": item[3] in PAD_SHAPES and not _items(item, "padstack")})
    tracks = defaultdict(list)
    vias = defaultdict(list)
    for item in _items(board, "segment"):
        tracks[net(item)].append((_field(item, "layer"), xy(item, "start"), xy(item, "end")))
    for item in _items(board, "via"):
        vias[net(item)].append((xy(item, "at"), layers(item)))
    return pads, tracks, vias, {net(z) for z in _items(board, "zone")}, {net(a) for a in _items(board, "arc")}


def _inside(pad, node):
    layer, x, y = node
    if layer not in pad["layers"] or not pad["geometry_known"]:
        return False
    x, y = x - pad["x"], y - pad["y"]
    a = pad["angle"]
    return _pad_distance({**pad, "x": 0, "y": 0}, x * cos(a) - y * sin(a), x * sin(a) + y * cos(a)) <= EPSILON


def _graph(tracks, vias, pads):
    graph = defaultdict(dict)
    points = defaultdict(set)
    for layer, a, b in tracks:
        points[layer].update((a, b))
    for point, layers in vias:
        for layer in layers:
            points[layer].add(point)
        if len(layers) == 2:
            a, b = ((layer, *point) for layer in layers)
            graph[a][b] = graph[b][a] = (0, 1)
    for layer, a, b in tracks:
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        nodes = sorted((p for p in points[layer]
                        if x0 - EPSILON <= p[0] <= x1 + EPSILON and y0 - EPSILON <= p[1] <= y1 + EPSILON
                        and _segment_distance(*p, a, b) <= EPSILON), key=lambda p: dist(a, p))
        for first, second in zip(nodes, nodes[1:]):
            x, y = (layer, *first), (layer, *second)
            graph[x][y] = graph[y][x] = (dist(first, second), 0)
    terminals = defaultdict(set)
    for p in pads:
        hits = {(layer, *xy) for layer in p["layers"] for xy in points[layer] if _inside(p, (layer, *xy))}
        terminals[p["ref"], p["number"]].update(hits)
        # Multiple entries on the same physical pad are joined by the pad copper.
        if hits:
            first = min(hits)
            for node in hits - {first}:
                if node not in graph[first]:
                    graph[first][node] = graph[node][first] = (0, 0)
    return graph, terminals


def _shortest(graph, source, goal):
    queue = []
    for start in source:
        heappush(queue, (0, 0, start))
    visited = set()
    while queue:
        length, vias, node = heappop(queue)
        if node in goal:
            return round(length, 4), vias
        if node in visited:
            continue
        visited.add(node)
        for neighbor, (edge, change) in graph[node].items():
            if neighbor not in visited:
                heappush(queue, (length + edge, vias + change, neighbor))
    return None


def _shared_branch(graph, shunt, sense, others):
    """Look for another load/power pad reachable from sense without crossing the shunt pad."""
    queue = deque(sorted(sense - shunt))
    visited = set(shunt)
    while queue:
        node = queue.popleft()
        if node in others:
            return True
        if node not in visited:
            visited.add(node)
            queue.extend(p for p in graph[node] if p not in visited)
    return False


def check_routes(board, constraints):
    if not constraints:
        return []
    pads, tracks, vias, zones, arcs = _read_copper(board)
    uncertain = zones | arcs | {p["net"] for p in pads if not p["geometry_known"]}
    pairs = plan_pairs(pads, constraints)
    nets = {p["net"] for p in pairs}
    copper = {net: _graph(tracks[net], vias[net], [p for p in pads if p["net"] == net]) for net in nets}
    findings = []
    for pair in pairs:
        a, b, net, kind = (pair[k] for k in ("a", "b", "net", "kind"))
        graph, terminals = copper[net]
        metric = _shortest(graph, terminals[a], terminals[b])
        status = "not_assessed"
        detail = "No complete centreline path observed; inspect pads, zones and native DRC."
        if kind == "kelvin":
            other_pads = set().union(*(nodes for pin, nodes in terminals.items() if pin not in (a, b)))
            if _shared_branch(graph, terminals[a], terminals[b], other_pads):
                status, detail = "failed", "Sense input reaches another pad off the shunt pad; move the Kelvin takeoff."
            elif metric and net not in uncertain:
                status, detail = "passed", "Direct shunt-pad takeoff in track graph; verify return and sense routing separately."
        elif metric:
            status, detail = "passed", "Copper path observed; length and layer changes are diagnostics, not electrical approval."
        elif net not in uncertain and not tracks[net] and not vias[net]:
            pa, pb = [[p for p in pads if (p["ref"], p["number"]) == pin] for pin in (a, b)]
            if not any(set(x["layers"]) & set(y["layers"]) and
                       dist((x["x"], x["y"]), (y["x"], y["y"])) <= (max(x["w"], x["h"]) + max(y["w"], y["h"])) / 2
                       for x in pa for y in pb):
                status, detail = "failed", "No track, via or zone on this required local connection."
        findings.append({**pair, "id": "copper:" + pair["id"], "status": status, "detail": detail,
                         "track_length_mm": metric[0] if metric else None, "layer_changes": metric[1] if metric else None})
    return findings


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("board", type=Path)
    parser.add_argument("constraints", type=Path)
    args = parser.parse_args()
    checks = check_routes(args.board, read_constraints(args.constraints))
    statuses = {check["status"] for check in checks}
    status = "failed" if "failed" in statuses else "needs_review" if not checks or "not_assessed" in statuses else "passed"
    print(json.dumps({"status": status, "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if status == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
