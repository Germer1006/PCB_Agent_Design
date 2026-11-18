"""Grid-based, clearance-aware two-layer routing for small boards."""

from __future__ import annotations

from collections import defaultdict
from heapq import heappop, heappush
from math import ceil, floor, hypot, isfinite, sqrt
from time import monotonic


DIAGONAL = sqrt(2)
INFINITY = float("inf")
MOVES = (
    (-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
    (-1, -1, DIAGONAL), (-1, 1, DIAGONAL),
    (1, -1, DIAGONAL), (1, 1, DIAGONAL),
)


def _pad_distance(pad, x, y):
    """Copper distance, conservatively using a rectangle for unknown shapes."""
    dx, dy = abs(x - pad["x"]), abs(y - pad["y"])
    half_w, half_h = pad["w"] / 2, pad["h"] / 2
    shape = pad.get("shape", "rect")
    if shape in ("circle", "oval"):
        radius = min(half_w, half_h)
    else:
        radius = 0.0
    if shape == "roundrect":
        radius = min(min(half_w, half_h), max(0.0, pad.get("radius", 0.0)))
    return max(0.0, hypot(max(0.0, dx - half_w + radius),
                          max(0.0, dy - half_h + radius)) - radius)


def _segment_distance(x, y, start, end):
    ax, ay = start
    dx, dy = end[0] - ax, end[1] - ay
    length_squared = dx * dx + dy * dy
    if not length_squared:
        return hypot(x - ax, y - ay)
    ratio = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length_squared))
    return hypot(x - ax - ratio * dx, y - ay - ratio * dy)


class _Grid:
    def __init__(self, width, height, pitch, max_cells=650_000):
        self.width, self.height, self.pitch = width, height, pitch
        self.nx, self.ny = floor(width / pitch) + 1, floor(height / pitch) + 1
        self.area = self.nx * self.ny
        if not isinstance(max_cells, int) or isinstance(max_cells, bool) or not 1 <= max_cells <= 4_000_000:
            raise ValueError("max_cells 应为 1–4000000 的整数。")
        if self.area > max_cells:
            raise ValueError(f"布线网格超过 {max_cells} 个位置，请增大网格间距或显式提高网格预算。")

    def node(self, ix, iy, layer):
        return layer * self.area + iy * self.nx + ix

    def unpack(self, node):
        layer, index = divmod(node, self.area)
        iy, ix = divmod(index, self.nx)
        return ix, iy, layer

    def point(self, node):
        ix, iy, _ = self.unpack(node)
        return [round(ix * self.pitch, 6), round(iy * self.pitch, 6)]

    def cells(self, x0, y0, x1, y1):
        pitch = self.pitch
        xmin = max(0, ceil(x0 / pitch - 1e-9))
        xmax = min(self.nx - 1, floor(x1 / pitch + 1e-9))
        ymin = max(0, ceil(y0 / pitch - 1e-9))
        ymax = min(self.ny - 1, floor(y1 / pitch + 1e-9))
        for iy in range(ymin, ymax + 1):
            row = iy * self.nx
            for ix in range(xmin, xmax + 1):
                yield row + ix, ix * pitch, iy * pitch

    def limits(self, inset):
        return (max(0, ceil(inset / self.pitch)),
                min(self.nx - 1, floor((self.width - inset) / self.pitch)),
                max(0, ceil(inset / self.pitch)),
                min(self.ny - 1, floor((self.height - inset) / self.pitch)))


def _mark_pad(grid, blocked, layers, pad, radius):
    x, y = pad["x"], pad["y"]
    dx, dy = pad["w"] / 2 + radius, pad["h"] / 2 + radius
    for index, cx, cy in grid.cells(x - dx, y - dy, x + dx, y + dy):
        if _pad_distance(pad, cx, cy) < radius + 1e-8:
            for layer in layers:
                blocked[index + layer * grid.area] = 1


def _mark_segment(grid, blocked, layers, start, end, radius):
    for index, x, y in grid.cells(
        min(start[0], end[0]) - radius, min(start[1], end[1]) - radius,
        max(start[0], end[0]) + radius, max(start[1], end[1]) + radius,
    ):
        if _segment_distance(x, y, start, end) < radius + 1e-8:
            for layer in layers:
                blocked[index + layer * grid.area] = 1


def _obstacles(grid, pads, segments, vias, net, width, clearance, via_diameter):
    track_blocked = bytearray(2 * grid.area)
    via_blocked = bytearray(grid.area)
    # A point along any grid edge is no more than half an edge from a grid node.
    track_radius = width / 2 + clearance + grid.pitch * DIAGONAL / 2 + 0.006
    via_radius = via_diameter / 2 + clearance + 0.006
    for pad in pads:
        if pad["net"] != net:
            _mark_pad(grid, track_blocked, pad["layers"] or (0, 1), pad, track_radius)
        # Avoid drilling any pad. Copper on the other layer is also relevant.
        _mark_pad(grid, via_blocked, (0,), pad,
                  via_radius if pad["net"] != net else via_diameter / 2 + 0.08)
    for segment in segments:
        if segment["net"] == net:
            continue
        start, end = segment["start"], segment["end"]
        radius = segment["width"] / 2
        _mark_segment(grid, track_blocked, (segment["layer"],), start, end, track_radius + radius)
        _mark_segment(grid, via_blocked, (0,), start, end, via_radius + radius)
    for via in vias:
        center = (via["x"], via["y"])
        if via["net"] != net:
            _mark_segment(grid, track_blocked, (0, 1), center, center, track_radius + via["size"] / 2)
        _mark_segment(grid, via_blocked, (0,), center, center,
                      (via_radius if via["net"] != net else via_diameter / 2 + 0.1) + via["size"] / 2)
    return track_blocked, via_blocked


def _landings(grid, pad, blocked, limits):
    """Pick nearest lattice point per allowed copper layer."""
    x, y = pad["x"], pad["y"]
    reach = grid.pitch / DIAGONAL + 1e-8
    result = {}
    xmin, xmax, ymin, ymax = limits
    for index, sx, sy in grid.cells(x - reach, y - reach, x + reach, y + reach):
        iy, ix = divmod(index, grid.nx)
        distance = hypot(sx - x, sy - y)
        if distance > reach or not (xmin <= ix <= xmax and ymin <= iy <= ymax):
            continue
        for layer in pad["layers"]:
            node = layer * grid.area + index
            if not blocked[node] and (layer not in result or distance < result[layer][0]):
                result[layer] = distance, node
    return [result[layer][1] for layer in sorted(result)]


def _fanout_stubs(geometry, pads, segments, vias, widths, clearance, edge_margin, deadline,
                  ground_escape_width, eligible_nets, priorities):
    """Connect dense SMD rows to the routing grid with short, checked copper stubs."""
    groups = defaultdict(list)
    for pad in pads:
        if pad.get("ref"):
            groups[pad["ref"]].append(pad)
    escapes = {}
    # The coarse search also needs clearance around the hand-off node.
    keep_landings = defaultdict(list)
    guard = geometry.pitch * DIAGONAL / 2 + .006
    for row in groups.values():
        cx = (min(p["x"] for p in row) + max(p["x"] for p in row)) / 2
        cy = (min(p["y"] for p in row) + max(p["y"] for p in row)) / 2
        for pad in sorted(row, key=lambda p: priorities.get(p["net"], len(priorities))):
            if monotonic() >= deadline:
                return escapes
            net = pad["net"]
            if net not in eligible_nets or len(pad["layers"]) != 1 or pad.get("drill", 0):
                continue
            axis, cross = ("x", "y") if pad["w"] > pad["h"] else ("y", "x")
            length, breadth = sorted((pad["h"], pad["w"]), reverse=True)
            width = widths.get(net, .25)
            if ground_escape_width is not None and net.rsplit("/", 1)[-1].upper() in {"GND", "PGND", "AGND"}:
                width = min(width, ground_escape_width)
            track_radius = width / 2 + clearance + guard
            # Look for a neighbour in the same physical pad row, on a different net.
            if length < 1.4 * breadth or not any(
                other is not pad and other["net"] != net
                and abs(other[axis] - pad[axis]) < .015
                and _pad_distance(other, pad["x"], pad["y"]) < track_radius
                for other in row
            ):
                continue
            delta = pad[axis] - (cx if axis == "x" else cy)
            if abs(delta) < length / 2:
                continue
            direction = 1 if delta > 0 else -1
            layer = pad["layers"][0]
            route_width = widths.get(net, .25)
            sweep = length / 2 + 1.2 + 4 * geometry.pitch + route_width / 2
            nearby = _nearby_copper(pad, pads, segments, vias, layer, clearance, sweep)
            reserved = [(point, point, radius) for point, radius, owner in keep_landings[layer] if owner != net]
            for node, points in _escape_options(geometry, pad, axis, cross, direction, length, edge_margin + route_width / 2):
                end = points[-1]
                if not _stub_clear([end, end], route_width / 2 + clearance + guard, nearby):
                    continue
                if not _stub_clear(points, width / 2 + clearance, (nearby[0], nearby[1] + reserved)):
                    continue
                trace = []
                for a, b in zip(points, points[1:]):
                    _add_segment(trace, net, layer, a, b, width)
                segments.extend(trace)
                escapes[id(pad)] = (node, trace)
                keep_landings[layer].append((end, route_width / 2 + guard, net))
                break
    return escapes


def _escape_options(geometry, pad, axis, cross, direction, length, inset):
    pitch = geometry.pitch
    tick = ceil if direction > 0 else floor
    p = tick((pad[axis] + direction * (length / 2 + pitch)) / pitch)
    q = round(pad[cross] / pitch)
    xmin, xmax, ymin, ymax = geometry.limits(inset)
    for steps in range(1 + ceil(1.2 / pitch)):
        u = p + steps * direction
        # Moving off the nearest grid column can leave the adjacent pin a clean channel.
        for cross_index in (q, q - 1, q + 1):
            ix, iy = (u, cross_index) if axis == "x" else (cross_index, u)
            if not (xmin <= ix <= xmax and ymin <= iy <= ymax):
                continue
            node = geometry.node(ix, iy, pad["layers"][0])
            end = geometry.point(node)
            side_shift = abs(cross_index * pitch - pad[cross])
            bend = [pad["x"], pad["y"]]
            bend[0 if axis == "x" else 1] = round(u * pitch - direction * side_shift, 6)
            if direction * (bend[0 if axis == "x" else 1] - pad[axis]) < length / 2:
                continue
            yield node, [[pad["x"], pad["y"]], bend, end]


def _nearby_copper(pad, pads, segments, vias, layer, clearance, sweep):
    px, py = pad["x"], pad["y"]
    net = pad["net"]
    near_pads, near_tracks = [], []
    for other in pads:
        if other["net"] != net and layer in (other["layers"] or (0, 1)):
            limit = sweep + clearance + max(other["w"], other["h"]) / 2
            if abs(other["x"] - px) <= limit and abs(other["y"] - py) <= limit:
                near_pads.append(other)
    for track in segments:
        if track["layer"] == layer and track["net"] != net:
            start, end, radius = track["start"], track["end"], track["width"] / 2
            if _segment_distance(px, py, start, end) <= sweep + clearance + radius:
                near_tracks.append((start, end, radius))
    for via in vias:
        if via["net"] != net:
            point = (via["x"], via["y"])
            radius = via["size"] / 2
            if hypot(point[0] - px, point[1] - py) <= sweep + clearance + radius:
                near_tracks.append((point, point, radius))
    return near_pads, near_tracks


def _stub_clear(points, track_clearance, nearby):
    pads, tracks = nearby
    for a, b in zip(points, points[1:]):
        steps = max(1, ceil(hypot(b[0] - a[0], b[1] - a[1]) / .02))
        # Distance changes by at most the distance to the nearest sampled point.
        margin = track_clearance + .01
        for i in range(steps + 1):
            x = a[0] + (b[0] - a[0]) * i / steps
            y = a[1] + (b[1] - a[1]) * i / steps
            if any(_pad_distance(pad, x, y) < margin for pad in pads):
                return False
            if any(_segment_distance(x, y, start, end) < margin + radius for start, end, radius in tracks):
                return False
    return True


def _search(grid, starts, goals, blocked, via_blocked, track_limits, via_limits, deadline):
    if not starts or not goals:
        return None
    if any(start in goals for start in starts):
        return [next(start for start in starts if start in goals)]
    points = tuple(grid.unpack(node) for node in goals)
    # A small exact goal set is more informative than a bounding box.
    if len(points) <= 6:
        def estimate(ix, iy, layer):
            return min(max(abs(ix - gx), abs(iy - gy))
                       + (DIAGONAL - 1) * min(abs(ix - gx), abs(iy - gy))
                       + (0 if layer == gl else 5.0) for gx, gy, gl in points)
    else:
        xs, ys, goal_layers = zip(*points)
        gx0, gx1, gy0, gy1 = min(xs), max(xs), min(ys), max(ys)
        layers = frozenset(goal_layers)

        def estimate(ix, iy, layer):
            dx, dy = max(gx0 - ix, ix - gx1, 0), max(gy0 - iy, iy - gy1, 0)
            return max(dx, dy) + (DIAGONAL - 1) * min(dx, dy) + (0 if layer in layers else 5.0)

    xmin, xmax, ymin, ymax = track_limits
    vxmin, vxmax, vymin, vymax = via_limits
    costs = {}
    parent = {}
    queue = []
    area, nx = grid.area, grid.nx
    for node in starts:
        ix, iy, layer = grid.unpack(node)
        cost = layer * 0.08
        costs[node] = cost
        heappush(queue, (cost + 1.10 * estimate(ix, iy, layer), cost, node))

    iterations = 0
    while queue:
        _, cost, node = heappop(queue)
        if costs.get(node) != cost:
            continue
        if node in goals:
            path = [node]
            while node in parent:
                node = parent[node]
                path.append(node)
            return path[::-1]
        iterations += 1
        if iterations % 256 == 0 and monotonic() >= deadline:
            return None
        index = node % area
        iy, ix = divmod(index, nx)
        layer = node // area
        track_cost = 1.0 + 0.035 * layer
        for dx, dy, delta in MOVES:
            neighbor_x, neighbor_y = ix + dx, iy + dy
            if not (xmin <= neighbor_x <= xmax and ymin <= neighbor_y <= ymax):
                continue
            neighbor = node + dy * nx + dx
            if blocked[neighbor]:
                continue
            new_cost = cost + delta * track_cost
            if new_cost < costs.get(neighbor, INFINITY):
                costs[neighbor] = new_cost
                parent[neighbor] = node
                score = new_cost + 1.10 * estimate(neighbor_x, neighbor_y, layer)
                heappush(queue, (score, new_cost, neighbor))
        if (vxmin <= ix <= vxmax and vymin <= iy <= vymax and not via_blocked[index]):
            neighbor = index + (1 - layer) * area
            new_cost = cost + max(5.0, 1.8 / grid.pitch)
            if not blocked[neighbor] and new_cost < costs.get(neighbor, INFINITY):
                costs[neighbor] = new_cost
                parent[neighbor] = node
                score = new_cost + 1.10 * estimate(ix, iy, 1 - layer)
                heappush(queue, (score, new_cost, neighbor))
    return None


def _add_segment(segments, net, layer, start, end, width):
    if start != end:
        segments.append({"net": net, "layer": layer, "start": start, "end": end, "width": width})


def _land(grid, pad, node, segments, net, width):
    _add_segment(segments, net, node // grid.area, [pad["x"], pad["y"]], grid.point(node), width)


def _emit_path(grid, path, net, width, via_diameter, via_drill, segments, vias):
    if len(path) < 2:
        return
    previous = path[0]
    start = previous
    last_step = None
    for node in path[1:]:
        layer = previous // grid.area
        if node // grid.area != layer:
            _add_segment(segments, net, layer, grid.point(start), grid.point(previous), width)
            x, y = grid.point(previous)
            vias.append({"net": net, "x": x, "y": y, "size": via_diameter, "drill": via_drill})
            start, last_step = node, None
        else:
            step = node - previous
            if last_step is not None and last_step != step:
                _add_segment(segments, net, layer, grid.point(start), grid.point(previous), width)
                start = previous
            last_step = step
        previous = node
    _add_segment(segments, net, previous // grid.area, grid.point(start), grid.point(previous), width)


def route_board(
    pads, board_width, board_height, *, widths=None, clearance=0.2, grid=0.15,
    via_diameter=0.65, via_drill=0.3, edge_margin=0.3, order=None, priority_pairs=(), timeout=18,
    max_cells=650_000, ground_escape_width=None,
):
    """Route all multi-pad nets; return segments, vias and any incomplete net names."""
    dimensions = (board_width, board_height, grid, via_diameter, via_drill, timeout)
    if not all(isfinite(value) and value > 0 for value in dimensions):
        raise ValueError("板尺寸、网格、过孔尺寸和时限需要为正数。")
    if not all(isfinite(value) and value >= 0 for value in (clearance, edge_margin)):
        raise ValueError("铜间距和板边距需要为非负数。")
    if via_diameter <= via_drill:
        raise ValueError("过孔外径必须大于钻孔直径。")
    widths = widths or {}
    if ground_escape_width is not None and (not isfinite(ground_escape_width) or ground_escape_width <= 0):
        raise ValueError("接地扇出线宽需要为正数。")
    if not all(isfinite(value) and value > 0 for value in widths.values()):
        raise ValueError("走线宽度需要为正数。")
    geometry = _Grid(board_width, board_height, grid, max_cells)
    signals = defaultdict(list)
    for pad in pads:
        if pad["net"]:
            signals[pad["net"]].append(pad)
    multi = {name: net_pads for name, net_pads in signals.items() if len(net_pads) > 1}
    rank = {name: index for index, name in enumerate(order or ())}
    nets = sorted(multi, key=lambda name: (rank.get(name, len(rank)), -widths.get(name, 0.25), name))
    segments, vias, unrouted = [], [], []
    deadline = monotonic() + timeout
    via_limits = geometry.limits(edge_margin + via_diameter / 2)
    by_pin = defaultdict(list)
    if priority_pairs:
        for pad in pads:
            by_pin[pad["ref"], pad["number"]].append(pad)
    reserved = defaultdict(list)
    kelvin_paths = defaultdict(list)
    priority_results = []
    fanouts = {}
    unused_stubs = set()
    failures = defaultdict(list)

    def reserve_fanout():
        nonlocal fanouts
        fanouts = _fanout_stubs(geometry, pads, segments, vias, widths, clearance,
                               edge_margin, deadline, ground_escape_width, multi.keys() - kelvin_paths.keys(), rank)
        unused_stubs.update(id(track) for _, trace in fanouts.values() for track in trace)

    def landings(pad, track_blocked, track_limits):
        escape = fanouts.get(id(pad))
        if escape is not None:
            node, _ = escape
            return [] if track_blocked[node] else [node]
        return _landings(geometry, pad, track_blocked, track_limits)

    def land(pad, node, net, width):
        escape = fanouts.get(id(pad))
        if escape is not None and node == escape[0]:
            unused_stubs.difference_update(id(track) for track in escape[1])
        else:
            _land(geometry, pad, node, segments, net, width)

    def obstacles(net, width):
        track_blocked, via_blocked = _obstacles(
            geometry, pads, segments, vias, net, width, clearance, via_diameter,
        )
        # Reserve Kelvin copper even against tracks on the same net, except within the shunt pad.
        for shunt, sense, trace, trace_vias in kelvin_paths[net]:
            blocked = bytearray(2 * geometry.area)
            blocked_vias = bytearray(geometry.area)
            margin = grid * DIAGONAL / 2 + 0.006
            _mark_pad(geometry, blocked, sense["layers"], sense, width / 2 + margin)
            for segment in trace:
                start, end = segment["start"], segment["end"]
                _mark_segment(geometry, blocked, (segment["layer"],), start, end,
                              (width + segment["width"]) / 2 + margin)
                _mark_segment(geometry, blocked_vias, (0,), start, end,
                              (via_diameter + segment["width"]) / 2 + margin)
            for via in trace_vias:
                center = (via["x"], via["y"])
                _mark_segment(geometry, blocked, (0, 1), center, center,
                              (width + via["size"]) / 2 + margin)
            sx, sy = shunt["x"], shunt["y"]
            hw, hh = shunt["w"] / 2, shunt["h"] / 2
            for index, x, y in geometry.cells(sx - hw, sy - hh, sx + hw, sy + hh):
                if _pad_distance(shunt, x, y) <= 1e-8:
                    for layer in shunt["layers"]:
                        blocked[index + layer * geometry.area] = 0
            track_blocked = bytearray(a | b for a, b in zip(track_blocked, blocked))
            via_blocked = bytearray(a | b for a, b in zip(via_blocked, blocked_vias))
        return track_blocked, via_blocked

    def emit(path, net, width, via_blocked):
        new_segments, new_vias = [], []
        _emit_path(geometry, path, net, width, via_diameter, via_drill, new_segments, new_vias)
        if any(
            hypot(first["x"] - second["x"], first["y"] - second["y"]) < via_drill + clearance
            for index, first in enumerate(new_vias)
            for second in new_vias[:index]
        ):
            return False
        segments.extend(new_segments)
        vias.extend(new_vias)
        for via in new_vias:
            center = (via["x"], via["y"])
            _mark_segment(geometry, via_blocked, (0,), center, center, via_diameter + 0.10)
        return True

    # Reserve PCB space for short local links before completing shared rails and ordinary signals.
    fanout_started = False
    for pair in priority_pairs:
        if pair["kind"] == "decoupling" and not fanout_started:
            reserve_fanout()
            fanout_started = True
        net = pair["net"]
        first, second = (by_pin[tuple(pair[k])] for k in ("a", "b"))
        result = {"id": pair["id"], "net": net, "status": "not_routed"}
        priority_results.append(result)
        if len(first) != 1 or len(second) != 1 or monotonic() >= deadline:
            continue
        first, second = first[0], second[0]
        members = {id(first), id(second)}
        fragments = reserved[net]
        if any(members <= linked for linked, _ in fragments):
            result["status"] = "already_linked"
            continue
        width = widths.get(net, 0.25)
        track_limits = geometry.limits(edge_margin + width / 2)
        track_blocked, via_blocked = obstacles(net, width)
        goals = landings(first, track_blocked, track_limits)
        starts = landings(second, track_blocked, track_limits)
        # A stuck critical pair must not consume the entire normal routing budget.
        pair_deadline = min(deadline, monotonic() + max(0.15, timeout / max(4, len(priority_pairs))))
        path = _search(geometry, starts, goals, track_blocked, via_blocked,
                       track_limits, via_limits, pair_deadline)
        segment_start, via_start = len(segments), len(vias)
        if path is None or not emit(path, net, width, via_blocked):
            result["reason"] = ("no_landing" if not starts or not goals else
                                "net_budget" if monotonic() >= pair_deadline else
                                "no_path" if path is None else "via_spacing")
            continue
        land(first, path[-1], net, width)
        land(second, path[0], net, width)
        # A Kelvin branch terminates at the shunt; its sense trace must not seed a power tree.
        if pair["kind"] == "kelvin":
            kelvin_paths[net].append((first, second, segments[segment_start:], vias[via_start:]))
        nodes = {path[-1]} if pair["kind"] == "kelvin" else set(path)
        for linked, copper in list(fragments):
            if members & linked:
                members.update(linked)
                nodes.update(copper)
                fragments.remove((linked, copper))
        fragments.append((members, nodes))
        result["status"] = "routed"

    if not fanout_started:
        reserve_fanout()

    def connect_net(net, connection_deadline, *, repair=False):
        """Complete a net from its existing copper islands, retaining partial progress."""
        fragments = reserved[net]
        expected = len(multi[net])
        if any(len(members) == expected for members, _ in fragments):
            return False
        width = widths.get(net, 0.25)
        track_limits = geometry.limits(edge_margin + width / 2)
        track_blocked, via_blocked = obstacles(net, width)
        fragment_by_pad = {key: (members, copper) for members, copper in fragments for key in members}
        candidates = [(pad, landings(pad, track_blocked, track_limits)) for pad in multi[net]]
        failures.pop(net, None)

        def record(pad, reason):
            failures[net].append({"ref": pad.get("ref"), "pin": pad.get("number"), "reason": reason})

        for pad, nodes in candidates:
            if not nodes and id(pad) not in fragment_by_pad:
                record(pad, "no_landing")
        candidates = [(pad, nodes) for pad, nodes in candidates if nodes or id(pad) in fragment_by_pad]
        if len(candidates) < 2:
            return True

        if fragments:
            members, copper = max(fragments, key=lambda fragment: len(fragment[0]))
            other_fragments = [(linked, nodes) for linked, nodes in fragments if linked is not members]
            connected = set(members)
            tree = set(copper)
            connected_pads = [pad for pad, _ in candidates if id(pad) in connected]
            candidates = [(pad, nodes) for pad, nodes in candidates if id(pad) not in connected]
            seed = None
        else:
            candidates.sort(key=lambda entry: (len(entry[1]), entry[0]["w"] * entry[0]["h"]))
            seed, seed_nodes = candidates.pop(0)
            connected = {id(seed)}
            tree = set(seed_nodes)
            connected_pads = [seed]
            other_fragments = []

        while candidates:
            if monotonic() >= connection_deadline:
                for pad, _ in candidates:
                    record(pad, "net_budget")
                break

            def rank_pad(entry):
                pad, starts = entry
                proximity = min(hypot(pad["x"] - p["x"], pad["y"] - p["y"]) for p in connected_pads)
                return (len(starts), proximity) if repair else (proximity, len(starts))

            best_index = min(range(len(candidates)), key=lambda i: rank_pad(candidates[i]))
            pad, starts = candidates.pop(best_index)
            linked, copper = fragment_by_pad.get(id(pad), (set(), set()))
            source = [node for node in sorted(copper) if not track_blocked[node]] if linked else starts
            path = _search(geometry, source, {node for node in tree if not track_blocked[node]}, track_blocked, via_blocked,
                           track_limits, via_limits, connection_deadline)
            if path is None:
                record(pad, "net_budget" if monotonic() >= connection_deadline else "no_path")
                continue
            if not emit(path, net, width, via_blocked):
                record(pad, "via_spacing")
                continue
            if seed is not None:
                for node in seed_nodes:
                    land(seed, node, net, width)
                seed = None
            if linked:
                tree.update(copper)
                connected.update(linked)
                other_fragments.remove((linked, copper))
                connected_pads.extend(other for other, _ in candidates if id(other) in linked)
                candidates = [(other, nodes) for other, nodes in candidates if id(other) not in linked]
            else:
                for node in starts:
                    land(pad, node, net, width)
                tree.update(starts)
                connected.add(id(pad))
            tree.update(path)
            connected_pads.append(pad)

        if seed is None:
            reserved[net] = [(connected, tree), *other_fragments]
        return len(connected) < expected

    start = monotonic()
    pass_deadline = start + .82 * max(0, deadline - start)
    for index, net in enumerate(nets):
        available = pass_deadline - monotonic()
        if available <= 0:
            unrouted.extend(n for n in nets[index:] if not any(
                len(members) == len(multi[n]) for members, _ in reserved[n]))
            break
        share = available / (len(nets) - index)
        limit = monotonic() + max(.6, share * (1 + min(len(multi[net]), 9) / 3))
        if connect_net(net, min(pass_deadline, limit)):
            unrouted.append(net)

    remaining = []
    for index, net in enumerate(unrouted):
        available = deadline - monotonic()
        if available <= 0:
            remaining.extend(unrouted[index:])
            break
        share = available / (len(unrouted) - index)
        if connect_net(net, min(deadline, monotonic() + max(.4, 2 * share)), repair=True):
            remaining.append(net)
    unrouted = remaining
    return {"segments": [track for track in segments if id(track) not in unused_stubs],
            "vias": vias, "unrouted": unrouted, "priority_pairs": priority_results, "failures": dict(failures),
            "fanout": [{"ref": p.get("ref"), "pin": p.get("number"), "net": p["net"],
                        "used": not any(id(track) in unused_stubs for track in fanouts[id(p)][1])}
                       for p in pads if id(p) in fanouts]}
