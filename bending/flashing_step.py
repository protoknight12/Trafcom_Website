"""The folded 3D body of a /flashing cladding as a STEP solid (OpenCascade through cadquery-ocp; no Flask).

The cross-section at the start (y = 0) and at the far end (y = length) is built from the unfold's straight parts and bends - straight lines and true circular
arcs, the inner radius exactly the one asked for, the thickness constant - and the solid is a ruled loft between the two. A tapered part therefore has the same
slanted bend lines as its flat pattern. Not in the solid: holes, relief notches, corner cuts (they live in the DXF), hems (a 180 degree fold does not loft).
"""
import math

from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.GC import GC_MakeArcOfCircle
from OCP.gp import gp_Pnt


def _items(flanges, bends, key):
    """The path as [('line', length) | ('arc', signed angle in radians, radius of the neutral line)] from the unfold's rows (`key` picks the start or the far-end straight length)."""
    out = []
    for j, f in enumerate(flanges):
        out.append(('line', f[key]))
        if j < len(bends):
            b = bends[j]
            out.append(('arc', (1 if b['dir'] == 'up' else -1) * math.radians(b['angle']), b))
    return out


def _closed_wire(items, t, r_in):
    """The thick cross-section as one closed wire: the left offset of the path forward, the end cap, the right offset back, the start cap."""
    pos, th, left, right = (0.0, 0.0), 0.0, [], []   # offset edges as ('line', p, q) | ('arc', p, mid, q)
    n = lambda a: (-math.sin(a), math.cos(a))
    off = lambda p, a, d: (p[0] + n(a)[0] * d, p[1] + n(a)[1] * d)
    for it in items:
        if it[0] == 'line':
            q = (pos[0] + it[1] * math.cos(th), pos[1] + it[1] * math.sin(th))
            left.append(('line', off(pos, th, t / 2), off(q, th, t / 2)))
            right.append(('line', off(pos, th, -t / 2), off(q, th, -t / 2)))
            pos = q
        else:
            s, phi, rc = (1 if it[1] > 0 else -1), abs(it[1]), max(r_in, 0.01) + t / 2
            c = (pos[0] + s * rc * n(th)[0], pos[1] + s * rc * n(th)[1])
            at = lambda f: (c[0] + s * rc * math.sin(th + s * phi * f), c[1] - s * rc * math.cos(th + s * phi * f))
            for side, d in ((left, t / 2), (right, -t / 2)):
                side.append(('arc', off(at(0), th, d), off(at(0.5), th + s * phi * 0.5, d), off(at(1), th + s * phi, d)))
            pos, th = at(1), th + s * phi
    edges = list(left)
    edges.append(('line', left[-1][-1], right[-1][-1]))
    edges += [(e[0],) + tuple(reversed(e[1:])) for e in reversed(right)]
    edges.append(('line', right[0][1], left[0][1]))
    return edges


def _wire(edges, y):
    pt = lambda p: gp_Pnt(p[0], y, p[1])   # profile (x, z), length along Y
    w = BRepBuilderAPI_MakeWire()
    for e in edges:
        if e[0] == 'line':
            w.Add(BRepBuilderAPI_MakeEdge(pt(e[1]), pt(e[2])).Edge())
        else:
            w.Add(BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(pt(e[1]), pt(e[2]), pt(e[3])).Value()).Edge())
    return w.Wire()


def build(u, thickness):
    """-> an OCP solid for the unfold `u` (app._flashing_unfold) or ValueError with the reason."""
    if any(b.get('kind') == 'hem' for b in u['bends']):
        raise ValueError('Подгъвките (hem) не се записват в STEP - само профилът без тях.')
    r_in = u['bends'][0]['radius'] if u['bends'] else 0.0
    sections = []
    for key, y in (('straight', 0.0), ('straight_end', u['length'])):
        sections.append(_wire(_closed_wire(_items(u['flanges'], u['bends'], key), thickness, r_in), y))
    loft = BRepOffsetAPI_ThruSections(True, True)   # a solid, ruled
    for w in sections:
        loft.AddWire(w)
    loft.Build()
    if not loft.IsDone():
        raise ValueError('Тялото не може да се построи.')
    return loft.Shape()
