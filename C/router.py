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
    via_diameter=0.65, via_drill=0.3, edge_margin=0.3, order=None, timeout=18,
    max_cells=650_000,
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
    for index, net in enumerate(nets):
        if monotonic() >= deadline:
            unrouted.extend(nets[index:])
            break
        width = widths.get(net, 0.25)
        track_limits = geometry.limits(edge_margin + width / 2)
        track_blocked, via_blocked = _obstacles(
            geometry, pads, segments, vias, net, width, clearance, via_diameter,
        )
        candidates = [(pad, _landings(geometry, pad, track_blocked, track_limits)) for pad in multi[net]]
        missing_landing = any(not landings for _, landings in candidates)
        candidates = [(pad, landings) for pad, landings in candidates if landings]
        if len(candidates) < 2:
            unrouted.append(net)
            continue

        # Start at the most constrained pad. Connect subsequent pads to the routed tree.
        candidates.sort(key=lambda entry: (len(entry[1]), entry[0]["w"] * entry[0]["h"]))
        seed, seed_nodes = candidates.pop(0)
        tree = set(seed_nodes)
        connected_pads = [seed]
        net_failed = missing_landing
        connection_deadline = min(deadline, monotonic() + max(1.0, (deadline - monotonic()) * 0.68))
        while candidates:
            if monotonic() >= connection_deadline:
                net_failed = True
                break
            best_index = min(range(len(candidates)), key=lambda i: min(
                abs(candidates[i][0]["x"] - pad["x"]) + abs(candidates[i][0]["y"] - pad["y"])
                for pad in connected_pads
            ))
            pad, starts = candidates.pop(best_index)
            path = _search(geometry, starts, tree, track_blocked, via_blocked,
                           track_limits, via_limits, connection_deadline)
            if path is None:
                net_failed = True
                continue
            new_segments, new_vias = [], []
            _emit_path(geometry, path, net, width, via_diameter, via_drill, new_segments, new_vias)
            if any(
                hypot(first["x"] - second["x"], first["y"] - second["y"]) < via_drill + clearance
                for index, first in enumerate(new_vias)
                for second in new_vias[:index]
            ):
                net_failed = True
                continue
            if len(connected_pads) == 1:
                for node in seed_nodes:
                    _land(geometry, seed, node, segments, net, width)
            for node in starts:
                _land(geometry, pad, node, segments, net, width)
            segments.extend(new_segments)
            vias.extend(new_vias)
            for via in new_vias:
                center = (via["x"], via["y"])
                _mark_segment(geometry, via_blocked, (0,), center, center, via_diameter + 0.10)
            tree.update(starts)
            tree.update(path)
            connected_pads.append(pad)
        if net_failed:
            unrouted.append(net)
    return {"segments": segments, "vias": vias, "unrouted": unrouted}
