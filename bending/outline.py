"""Closed contours of a flat pattern (no Flask): the cut entities of analyze_dxf_geometry ({'type': 'line'|'circle'|'arc', ...}) -> loops of points, the outer
loop first. The 3D bend simulation folds these loops into the real part (holes, notches, rounded corners)."""
import math


def _arc_pts(s, step_deg=6.0):
    a0, a1 = math.radians(s['start_angle']), math.radians(s['end_angle'])
    sweep = (a1 - a0) % (2 * math.pi) or 2 * math.pi
    n = max(2, int(math.degrees(sweep) / step_deg) + 1)
    return [(s['cx'] + s['r'] * math.cos(a0 + sweep * i / n), s['cy'] + s['r'] * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]


def _area(loop):
    return sum(loop[i - 1][0] * loop[i][1] - loop[i][0] * loop[i - 1][1] for i in range(len(loop))) / 2


def loops_from_shapes(shapes, tol=0.05, max_points=20000):
    """-> [[(x, y), ...], ...] closed loops (the last point is not repeated), the one of the largest area first. Open chains (a broken drawing) are dropped.
    ponytail: greedy chaining by end points within `tol`; a loop that touches itself or another at one point may be chained wrongly."""
    loops, pieces = [], []
    for s in shapes:
        if s['type'] == 'circle':
            n = max(12, min(72, int(s['r'] * 4)))
            loops.append([(s['cx'] + s['r'] * math.cos(2 * math.pi * i / n), s['cy'] + s['r'] * math.sin(2 * math.pi * i / n)) for i in range(n)])
        elif s['type'] == 'arc':
            pieces.append(_arc_pts(s))
        else:
            pieces.append([(s['x1'], s['y1']), (s['x2'], s['y2'])])
    near = lambda p, q: math.dist(p, q) <= tol
    while pieces:
        chain = pieces.pop(0)
        grew = True
        while grew and not near(chain[0], chain[-1]):
            grew = False
            for i, pc in enumerate(pieces):
                if near(chain[-1], pc[0]):
                    chain += pc[1:]
                elif near(chain[-1], pc[-1]):
                    chain += pc[::-1][1:]
                elif near(chain[0], pc[-1]):
                    chain = pc[:-1] + chain
                elif near(chain[0], pc[0]):
                    chain = pc[::-1][:-1] + chain
                else:
                    continue
                pieces.pop(i)
                grew = True
                break
        if near(chain[0], chain[-1]) and len(chain) > 3:
            loops.append(chain[:-1])
    loops.sort(key=lambda l: -abs(_area(l)))
    return loops if sum(len(l) for l in loops) <= max_points else []


def extent_at(loop, w):
    """(c_min, c_max) of the closed loop of [c, w] points on the line across it at w (the part's two edges there; they are not parallel on a tapered part), or None."""
    cs = []
    for i in range(len(loop)):
        (c0, w0), (c1, w1) = loop[i - 1], loop[i]
        if (w0 <= w < w1) or (w1 <= w < w0):   # half-open: a vertex exactly on the line is counted once
            cs.append(c0 + (c1 - c0) * (w - w0) / (w1 - w0))
    return (min(cs), max(cs)) if cs else None


def line_c_at(line, w):
    """c of the bend line [c0, w0, c1, w1] at w (extended along its direction)."""
    c0, w0, c1, w1 = line
    return c0 if abs(w1 - w0) < 1e-9 else c0 + (c1 - c0) * (w - w0) / (w1 - w0)
