"""Bend steps from the bend lines of a flat-pattern DXF (no Flask). Layers BEND_UP / BEND_DOWN (the /flashing convention); an angle may
follow in the layer name (BEND_UP_45), else 90 is assumed, and then the inner radius in mm (BEND_UP_45_R2.5). Order and gauge side: see bending.sequence / bending.planner."""
import math
import re

from bending.sequence import steps_from_bends


def bend_axis(lines):
    """0 when most bend lines run along y (they sit at an x: the chain runs along x), else 1."""
    return 0 if sum(abs(p1[0] - p0[0]) <= abs(p1[1] - p0[1]) for _, p0, p1 in lines) * 2 >= len(lines) else 1


def bends_from_lines(lines, flat_min, flat_max):
    """lines: [(layer, (x0, y0), (x1, y1))]; flat_min/flat_max: corners of the cut outline. -> (bends [{pos (the middle of the line), line [c0, w0, c1, w1] (along the chain / across it, mm from the flat's start / its middle), angle, dir, length, radius?, note}], flat length)."""
    if not lines:
        return [], 0.0
    ax = bend_axis(lines)
    oa, mid = 1 - ax, (flat_min[1 - ax] + flat_max[1 - ax]) / 2
    bends = []
    for layer, p0, p1 in lines:
        rm = re.search(r'_R(\d+(?:\.\d+)?)$', layer.upper())   # the inner radius of the model, when the exporter knew it
        m = re.search(r'_(\d{1,3})(?:_R\d+(?:\.\d+)?)?$', layer.upper())
        ok = m and 0 < float(m.group(1)) <= 180
        a, b = (p0, p1) if p0[oa] <= p1[oa] else (p1, p0)   # the line from its low end to its high end across the chain (a tapered part's bend lines are slanted)
        bends.append({'line': [round(a[ax] - flat_min[ax], 3), round(a[oa] - mid, 3), round(b[ax] - flat_min[ax], 3), round(b[oa] - mid, 3)],
                      'pos': (p0[ax] + p1[ax]) / 2 - flat_min[ax], 'angle': float(m.group(1)) if ok else 90.0,
                      'dir': 'down' if 'DOWN' in layer.upper() else 'up', 'length': round(math.hypot(p1[0] - p0[0], p1[1] - p0[1]), 1), **({'radius': float(rm.group(1))} if rm else {}), 'note': None if ok else 'Ъгъл 90° по подразбиране - проверете'})
    return bends, flat_max[ax] - flat_min[ax]


def steps_from_bend_lines(lines, flat_min, flat_max):
    bends, flat = bends_from_lines(lines, flat_min, flat_max)
    return steps_from_bends(bends, flat) if bends else []
