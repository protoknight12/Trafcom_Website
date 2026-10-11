"""Bend steps from the bend lines of a flat-pattern DXF (no Flask). Layers BEND_UP / BEND_DOWN (the /flashing convention); an angle may
follow in the layer name (BEND_UP_45), else 90 is assumed. Order and gauge side: see bending.sequence / bending.planner."""
import math
import re

from bending.sequence import steps_from_bends


def bends_from_lines(lines, flat_min, flat_max):
    """lines: [(layer, (x0, y0), (x1, y1))]; flat_min/flat_max: corners of the cut outline. -> (bends [{pos, angle, dir, length (of the bend line), note}], flat length)."""
    if not lines:
        return [], 0.0
    vertical = sum(abs(p1[0] - p0[0]) <= abs(p1[1] - p0[1]) for _, p0, p1 in lines) * 2 >= len(lines)   # most lines run along y -> they sit at an x
    ax = 0 if vertical else 1
    bends = []
    for layer, p0, p1 in lines:
        m = re.search(r'_(\d{1,3})$', layer.upper())
        ok = m and 0 < float(m.group(1)) < 180
        bends.append({'pos': (p0[ax] + p1[ax]) / 2 - flat_min[ax], 'angle': float(m.group(1)) if ok else 90.0,
                      'dir': 'down' if 'DOWN' in layer.upper() else 'up', 'length': round(math.hypot(p1[0] - p0[0], p1[1] - p0[1]), 1), 'note': None if ok else 'Ъгъл 90° по подразбиране - проверете'})
    return bends, flat_max[ax] - flat_min[ax]


def steps_from_bend_lines(lines, flat_min, flat_max):
    bends, flat = bends_from_lines(lines, flat_min, flat_max)
    return steps_from_bends(bends, flat) if bends else []
