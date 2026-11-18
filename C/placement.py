"""Deterministic two-layer placement for small KiCad designs."""

from __future__ import annotations

import math
import random
from collections import defaultdict


ROTATIONS = (0, 90, 180, 270)


def rotate(x, y, angle):
    return ((x, y), (y, -x), (-x, -y), (-y, x))[angle // 90]


def placed_pads(design, placement):
    pads = []
    for component in design["components"]:
        position = placement[component["ref"]]
        angle = position["angle"]
        for pad in component["pads"]:
            x, y = rotate(pad["x"], pad["y"], angle)
            width, height = (pad["h"], pad["w"]) if angle in (90, 270) else (pad["w"], pad["h"])
            pads.append({
                **pad, "x": round(position["x"] + x, 6), "y": round(position["y"] + y, 6),
                "w": width, "h": height,
            })
    return pads


def boost_profile(design):
    """Infer standard power labels and the demonstrator's MT3608 power stage."""
    nets = design["nets"]
    weights = {}
    roles = {}
    pairs = []
    for name in nets:
        label = name.strip("/").rsplit("/", 1)[-1].upper()
        if label in {"GND", "PGND", "AGND"}:
            roles[label] = name
            weights[name] = 1.8
        elif label in {"VIN", "VIN_RAW", "VIN_F", "VBUS", "VBOOST", "VOUT", "SW", "LX", "FB", "ISENSE", "VSENSE"}:
            roles[label] = name
            weights[name] = 3.0
    converters = [part for part in design["components"] if part["value"].strip().upper() == "MT3608"]
    if len(converters) == 1:
        chip = converters[0]
        pins = {p["number"]: p["net"] for p in chip["pads"]}
        for role, pin in {"SW": "1", "GND": "2", "FB": "3", "VIN": "5"}.items():
            if pins.get(pin):
                roles[role] = pins[pin]
        switch = roles.get("SW")
        diodes = [
            part for part in design["components"]
            if part["ref"].startswith("D") and switch and any(p["net"] == switch for p in part["pads"])
        ]
        if len(diodes) == 1:
            diode = diodes[0]
            outputs = {p["net"] for p in diode["pads"] if p["net"] and p["net"] != switch}
            if len(outputs) == 1:
                output = outputs.pop()
                roles["VOUT"] = output
                caps = [
                    part for part in design["components"]
                    if part["ref"].startswith("C")
                    and {output, roles.get("GND")} <= {p["net"] for p in part["pads"]}
                ]
                diode_sw = next(p for p in diode["pads"] if p["net"] == switch)
                pairs.append(((chip["ref"], "1"), (diode["ref"], diode_sw["number"]), 4.0))
                if len(caps) == 1:
                    cap = caps[0]
                    out_pad = next(p for p in cap["pads"] if p["net"] == output)
                    gnd_pad = next(p for p in cap["pads"] if p["net"] == roles["GND"])
                    diode_out = next(p for p in diode["pads"] if p["net"] == output)
                    pairs.extend((
                        ((diode["ref"], diode_out["number"]), (cap["ref"], out_pad["number"]), 4.0),
                        ((cap["ref"], gnd_pad["number"]), (chip["ref"], "2"), 3.8),
                    ))
    for role, name in roles.items():
        weights[name] = max(weights.get(name, 1.0), {
            "SW": 4.5, "LX": 4.5, "FB": 3.5, "GND": 1.8, "VIN": 2.4, "VOUT": 2.7,
        }.get(role, 3.0))
    return {"roles": roles, "weights": weights, "pairs": pairs}


def board_size(components):
    area = sum((part["width"] + 1.4) * (part["height"] + 1.4) for part in components)
    height = max(25, math.ceil(math.sqrt(area / 0.36) / 2) * 2)
    width = max(34, math.ceil(height * 1.25 / 2) * 2)
    for part in components:
        width = max(width, math.ceil(part["width"] + 4))
        height = max(height, math.ceil(part["height"] + 4))
    return width, height


def place(design, width, height, profile, seed=0):
    rng = random.Random(seed)
    parts = {part["ref"]: part for part in design["components"]}
    weights = profile["weights"]
    constraints = profile.get("constraints", {})
    net_refs = defaultdict(set)
    net_pad_counts = defaultdict(int)
    for ref, part in parts.items():
        for pad in part["pads"]:
            if pad["net"]:
                net_refs[pad["net"]].add(ref)
                net_pad_counts[pad["net"]] += 1
    by_ref = defaultdict(set)
    for net, refs in net_refs.items():
        if len(refs) > 1:
            for ref in refs:
                by_ref[ref].add(net)
    connectors = sorted(ref for ref in parts if ref.startswith(("J", "P")) and not ref.startswith("JP"))
    anchor = {ref: (2.5 if index % 2 == 0 else width - 2.5, height / 2)
              for index, ref in enumerate(connectors[:2])}

    geometry = {}
    for ref, part in parts.items():
        for angle in ROTATIONS:
            corners = [
                rotate(x, y, angle)
                for x in (part["min_x"], part["max_x"])
                for y in (part["min_y"], part["max_y"])
            ]
            bounds = (min(x for x, _ in corners), min(y for _, y in corners),
                      max(x for x, _ in corners), max(y for _, y in corners))
            pins = [(p["number"], p["net"], *rotate(p["x"], p["y"], angle)) for p in part["pads"]]
            geometry[ref, angle] = bounds, pins

    def envelope(ref, position):
        x, y, angle = position
        x0, y0, x1, y1 = geometry[ref, angle][0]
        return x + x0, y + y0, x + x1, y + y1

    def fits(ref, position, placements):
        x0, y0, x1, y1 = envelope(ref, position)
        if min(x0, y0) < 1.0 or x1 > width - 1.0 or y1 > height - 1.0:
            return False
        for rule in constraints.get("regions", []):
            if ref in rule["refs"]:
                left, top, right, bottom = rule["bounds_mm"]
                if x0 < left or y0 < top or x1 > right or y1 > bottom:
                    return False
        for rule in constraints.get("keepouts", []):
            if not rule["refs"] or ref in rule["refs"]:
                left, top, right, bottom = rule["bounds_mm"]
                if not (x1 <= left or right <= x0 or y1 <= top or bottom <= y0):
                    return False
        gap = 0.85
        for other, location in placements.items():
            if other == ref:
                continue
            ox0, oy0, ox1, oy1 = envelope(other, location)
            if not (x1 + gap <= ox0 or ox1 + gap <= x0 or y1 + gap <= oy0 or oy1 + gap <= y0):
                return False
        return True

    def score(placements):
        nets = defaultdict(list)
        endpoints = {}
        cost = 0.0
        for ref, (x, y, angle) in placements.items():
            for number, net, dx, dy in geometry[ref, angle][1]:
                if net and net_pad_counts[net] > 1:
                    location = x + dx, y + dy
                    nets[net].append(location)
                    endpoints[ref, number] = location
            if ref in anchor:
                ax, ay = anchor[ref]
                cost += 1.3 * abs(x - ax) + 0.20 * abs(y - ay)
        for net, positions in nets.items():
            if len(positions) > 1:
                xs, ys = zip(*positions)
                cost += weights.get(net, 1.0) * (max(xs) - min(xs) + max(ys) - min(ys))
        for first, second, factor in profile["pairs"]:
            if first in endpoints and second in endpoints:
                x1, y1 = endpoints[first]
                x2, y2 = endpoints[second]
                cost += factor * math.hypot(x2 - x1, y2 - y1)
        for pair in constraints.get("pad_pairs", []):
            first, second = tuple(pair["a"]), tuple(pair["b"])
            limit = pair.get("max_distance_mm")
            if limit is not None and first in endpoints and second in endpoints:
                excess = max(0.0, math.dist(endpoints[first], endpoints[second]) - limit)
                cost += 20 * pair.get("weight", 5.0) * excess ** 2
        for group in constraints.get("groups", []):
            points = [placements[ref] for ref in group["refs"] if ref in placements]
            if len(points) > 1:
                span = math.hypot(max(p[0] for p in points) - min(p[0] for p in points),
                                  max(p[1] for p in points) - min(p[1] for p in points))
                cost += group.get("weight", 5.0) * span
                if group.get("max_span_mm") is not None:
                    cost += 20 * group.get("weight", 5.0) * max(0, span - group["max_span_mm"]) ** 2
        return cost

    positions = {}
    left = set(parts)
    for ref, location in sorted(constraints.get("fixed", {}).items()):
        proposal = (location["x"], location["y"], location["angle"])
        if not fits(ref, proposal, positions):
            raise ValueError(f"固定器件 {ref} 越界、重叠或违反区域约束。")
        positions[ref] = proposal
        left.remove(ref)
    grid_x = list(range(2, int(width) - 1))
    grid_y = list(range(2, int(height) - 1))
    noise = {ref: rng.random() for ref in parts}
    while left:
        used_nets = {net for ref in positions for net in by_ref[ref]}
        ref = max(left, key=lambda item: (
            sum(weights.get(n, 1.0) * (2 if n in used_nets else 1) for n in by_ref[item]),
            parts[item]["width"] * parts[item]["height"],
            noise[item],
        ))
        preferred = anchor.get(ref, (width / 2, height / 2))
        best = None
        for x in grid_x:
            for y in grid_y:
                for angle in ROTATIONS:
                    proposal = (x, y, angle)
                    if not fits(ref, proposal, positions):
                        continue
                    positions[ref] = proposal
                    cost = score(positions) + 0.04 * math.hypot(x - preferred[0], y - preferred[1])
                    if seed:
                        cost += rng.uniform(0, 0.7)
                    del positions[ref]
                    if best is None or cost < best[0]:
                        best = cost, proposal
        if best is None:
            raise ValueError(f"板框内放不下 {ref}。请扩大尺寸或调整器件。")
        positions[ref] = best[1]
        left.remove(ref)

    current = score(positions)
    best_cost, best_positions = current, positions.copy()
    refs = sorted(set(positions) - set(constraints.get("fixed", {})))
    if not refs:
        return {ref: {"x": x, "y": y, "angle": angle} for ref, (x, y, angle) in positions.items()}
    iterations = min(2300, 140 * len(refs))
    for step in range(iterations):
        ref = rng.choice(refs)
        old = positions[ref]
        if rng.random() < 0.27:
            proposal = (old[0], old[1], rng.choice(ROTATIONS))
        else:
            proposal = (old[0] + rng.choice((-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0)),
                        old[1] + rng.choice((-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0)),
                        old[2])
        if not fits(ref, proposal, positions):
            continue
        positions[ref] = proposal
        cost = score(positions)
        temperature = 4 * (1 - step / iterations) + 0.10
        if cost <= current or rng.random() < math.exp((current - cost) / temperature):
            current = cost
            if cost < best_cost:
                best_cost, best_positions = cost, positions.copy()
        else:
            positions[ref] = old
    return {ref: {"x": x, "y": y, "angle": angle} for ref, (x, y, angle) in best_positions.items()}
