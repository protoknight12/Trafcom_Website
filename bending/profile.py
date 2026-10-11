"""Press-brake tool profiles (no Flask): outline with real radii, picture, DXF import, checks.

A tool's profile is its WORKING part only, as vertices [x, y, r, a] in mm (y up): r = fillet radius at the corner, a = radius of the
arc replacing the edge to the next vertex. The radii are data - calculations read them from here. The clamping part is standard
for the machine's beam (CLAMP) and is only added when drawing. A tool without a stored profile gets a starting one from its numbers.
"""
import math

PUNCH_HALF_WIDTH = 7.0  # body half-width, mm

# The clamping part is the same for every punch of the press brake's beam, so it is NOT part of a tool's stored outline
# (that ends at the seat line) and is never edited: it is added when drawing. European (Promecam) tang, read off the P.97 drawing.
CLAMP = {'name': 'european', 'width': 13.0, 'height': 30.0, 'chamfer': 0.86, 'notch_depth': 3.27, 'notch_from_seat': 4.65, 'notch_height': 8.27}


# The die channel of the press brakes is 60 mm wide: the BASE of every die (its bottom `base_height` mm) is exactly that wide. The die above
# the base may be narrower; the base height is a setting of the tool (BendTool.base_height_mm).
DIE_CHANNEL = 60.0


def width_at(dense, y):
    """Horizontal extent [min x, max x] of the outline on the line at height y (None when the line misses it)."""
    xs = []
    n = len(dense)
    for i in range(n):
        a, b = dense[i], dense[(i + 1) % n]
        if (a[1] - y) * (b[1] - y) <= 0 and a[1] != b[1]:
            xs.append(a[0] + (y - a[1]) / (b[1] - a[1]) * (b[0] - a[0]))
    return (min(xs), max(xs)) if xs else None


def tip_offset(angle_deg, radius):
    """Height of the lowest point of a rounded tip above the theoretical sharp apex (catalogue heights are measured to the apex)."""
    if not angle_deg or not radius:
        return 0.0
    return radius / math.sin(math.radians(angle_deg) / 2) - radius


def clamp_polygon(points):
    """The standard tang drawn on top of a punch's working outline: flush with the left edge of the seat line. [[x, y], ...] in mm."""
    c = CLAMP
    points = outline(points)
    seat = max(p[1] for p in points)
    xl = min(p[0] for p in points if seat - p[1] < 0.05)
    xr, y0, y1, k = xl + c['width'], seat, seat + c['height'], c['chamfer']
    n0, n1, d = y0 + c['notch_from_seat'], y0 + c['notch_from_seat'] + c['notch_height'], c['notch_depth']
    return [[xl, y0], [xr, y0], [xr, y1 - k], [xr - k, y1], [xl + k, y1], [xl, y1 - k], [xl, n1], [xl + d, n1], [xl + d, n0], [xl, n0]]


def clip_to_seat(points, seat):
    """Cut a full catalogue outline off above y = seat (Sutherland-Hodgman on the vertices) - drops the clamping part.
    An edge that crosses the seat line must be straight."""
    pts = normalize(points)
    if max(p[1] for p in pts) <= seat + 1e-9:
        return pts
    out, n = [], len(pts)
    for i, a in enumerate(pts):
        b = pts[(i + 1) % n]
        a_in, b_in = a[1] <= seat + 1e-9, b[1] <= seat + 1e-9
        if a_in:
            out.append(list(a))
        if a_in != b_in:
            if a[3]:
                raise ValueError('Дъга пресича линията на седлото - закрепването не може да се отреже.')
            t = (seat - a[1]) / (b[1] - a[1])
            out.append([round(a[0] + t * (b[0] - a[0]), 2), seat, 0.0, 0.0])
    return out


def _f(v):
    return f'{v:.2f}'.rstrip('0').rstrip('.')


def tool_svg(kind, angle_deg, radius, height, v, code='', profile=None, base_height=None):
    """Picture of a tool: its stored outline, or - for a tool without one yet - a starting outline built from its numbers."""
    return polygon_svg(profile or default_profile(kind, angle_deg, radius, height, v), kind, code, base_height)


# ---- real profiles (outline from a catalogue DXF) -------------------------------------------------------------

GRID_MM = 5.0
GRID_CELLS = 12  # a 60 x 60 mm square folded over the tip at 45 degrees: shows what part (leg length) fits beside the punch


def _grid_svg():
    """Raster of GRID_MM cells in a square rotated 45 degrees, its lower corner at the tip (the tool origin); y is up here."""
    side = GRID_CELLS * GRID_MM * math.sqrt(2)  # length of the square's side measured along p / q (= y -/+ x)
    step = GRID_MM * math.sqrt(2)
    lines = []
    for k in range(GRID_CELLS + 1):
        c = k * step
        lines.append((c, 0, c, side))   # p = c : q from 0 to side
        lines.append((0, c, side, c))   # q = c : p from 0 to side
    pt = lambda p, q: (_f((q - p) / 2), _f(-(p + q) / 2))  # p = y - x, q = y + x -> svg (x, -y)
    out = []
    for p0, q0, p1, q1 in lines:
        (xa, ya), (xb, yb) = pt(p0, q0), pt(p1, q1)
        out.append(f'M {xa} {ya} L {xb} {yb}')
    return f'<path d="{" ".join(out)}" fill="none" stroke="#6f7b87" stroke-width="0.25"/>'


def polygon_svg(points, kind, code='', base_height=None):
    """points: [[x, y], ...] in mm, y up, punch tip at the origin (die: top surface at y = 0, centred). A punch is drawn over the grid."""
    points = outline(points)  # fillets and arcs expanded
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    path = 'M ' + ' L '.join(f'{_f(x)} {_f(-y)}' for x, y in points) + ' Z'
    clamp = ''
    if kind == 'punch':
        cp = clamp_polygon(points)
        clamp = '<path d="M ' + ' L '.join(f'{_f(x)} {_f(-y)}' for x, y in cp) + ' Z" fill="#7d8a97" fill-opacity="0.75" stroke="#e0a526" stroke-opacity="0.5" stroke-width="0.4" stroke-dasharray="1.2 0.8"/>'
        xs, ys = xs + [x for x, _ in cp], ys + [y for _, y in cp]
    grid, extra = '', 0.0
    if kind == 'punch':
        half = GRID_CELLS * GRID_MM / math.sqrt(2) * math.sqrt(2) / 2 * math.sqrt(2)  # half of the diagonal
        grid = _grid_svg()
        x0, x1 = min(min(xs), -half) - 2, max(max(xs), half) + 2
        y0, y1 = -max(max(ys), 2 * half) - 3, 4
    else:
        x0, x1, y0, y1 = min(xs) - 3, max(xs) + 3, -max(ys) - 3, -min(ys) + 3
    base = ''
    if kind == 'die' and base_height:  # the base zone: its top edge, dashed
        yb = -(min(ys) + base_height)
        base = (f'<path d="M {_f(-DIE_CHANNEL / 2 - 1)} {_f(yb)} L {_f(DIE_CHANNEL / 2 + 1)} {_f(yb)}" stroke="#6fa8d6" stroke-width="0.4" stroke-dasharray="1.5 1" fill="none"/>')
    label = f'<text x="{_f(x0 + 2)}" y="{_f(y1 - 1)}" font-size="3.2" fill="#9aa5b1" font-family="Arial">{GRID_MM:g} mm</text>' if grid else ''
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_f(x0)} {_f(y0)} {_f(x1 - x0)} {_f(y1 - y0)}"><title>{code}</title>{grid}{clamp}'
            f'<path d="{path}" fill="#cfd8e0" fill-opacity="0.95" stroke="#e0a526" stroke-width="0.5" stroke-linejoin="round"/>{base}{label}</svg>')


def _bulge_arc(p, q, bulge):
    """Signed arc radius (> 0 bulges left of travel) for a polyline segment p -> q with the given bulge; minor arcs only."""
    if not bulge:
        return 0.0
    if abs(bulge) > 1:
        raise ValueError('Дъга над 180° не се поддържа - разделете я на две.')
    chord = math.hypot(q[0] - p[0], q[1] - p[1])
    r = chord * (1 + bulge * bulge) / (4 * abs(bulge))
    return -r if bulge > 0 else r  # a counter-clockwise arc (bulge > 0) bulges to the right of travel


def polygon_from_dxf(doc, kind):
    """Outline of a tool from a DXF: one closed polyline, or LINE / ARC / polyline pieces chaining into a loop. Arcs stay arcs with their
    radius. -> [[x, y, r, a], ...] in mm. A punch is expected tip down: origin = its lowest point; a die: centre of its top edge."""
    edges = []  # (start, end, signed arc radius)
    for e in doc.modelspace():
        t = e.dxftype()
        if t == 'LINE':
            edges.append(((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y), 0.0))
        elif t == 'ARC':
            sweep = (e.dxf.end_angle - e.dxf.start_angle) % 360
            if sweep > 180.0001:
                raise ValueError('Дъга над 180° не се поддържа - разделете я на две.')
            s_, e_ = e.start_point, e.end_point
            edges.append(((s_.x, s_.y), (e_.x, e_.y), -e.dxf.radius))  # ARC runs counter-clockwise: it bulges right of travel
        elif t in ('LWPOLYLINE', 'POLYLINE'):
            if t == 'LWPOLYLINE':
                vs = [(x, y, b) for x, y, _, _, b in e.get_points('xyseb')]
                closed = e.closed
            else:
                vs = [(v.dxf.location.x, v.dxf.location.y, v.dxf.bulge or 0.0) for v in e.vertices]
                closed = e.is_closed
            for i in range(len(vs) - (0 if closed else 1)):
                p, q = vs[i], vs[(i + 1) % len(vs)]
                edges.append(((p[0], p[1]), (q[0], q[1]), _bulge_arc(p, q, p[2])))
    if not edges:
        raise ValueError('В DXF няма контур (линии, дъги или полилиния).')
    close = lambda p, q: math.hypot(p[0] - q[0], p[1] - q[1]) < 0.05
    chain = [edges.pop(0)]
    while edges and not close(chain[0][0], chain[-1][1]):
        for i, (p, q, arc) in enumerate(edges):
            if close(p, chain[-1][1]):
                chain.append((p, q, arc))
            elif close(q, chain[-1][1]):
                chain.append((q, p, -arc))  # walked backwards: the bulge changes side
            else:
                continue
            edges.pop(i)
            break
        else:
            break
    if edges or len(chain) < 3 or not close(chain[0][0], chain[-1][1]):
        raise ValueError('Контурът не е затворен или има излишни елементи - нужен е един затворен профил.')
    pts = normalize([[p[0], p[1], 0, arc] for p, _, arc in chain])
    dense = outline(pts)
    if kind == 'punch':
        oy = min(p[1] for p in dense)
        tip = [p[0] for p in dense if p[1] - oy < 0.05]
        ox = sum(tip) / len(tip)
    else:
        oy = max(p[1] for p in dense)
        ox = (min(p[0] for p in dense) + max(p[0] for p in dense)) / 2
    return [[round(p[0] - ox, 2), round(p[1] - oy, 2), p[2], p[3]] for p in pts]


def validate_polygon(points):
    """Check an outline from the editor/DXF: 3..400 vertices within +-1000 mm, area >= 1 mm2, radii that fit (fillets/arcs) and no
    crossing edges (tested on the dense outline, so arcs count). Returns the normalised [[x, y, r, a], ...]; raises ValueError."""
    pts = normalize(points)
    dense = outline(pts)
    n = len(dense)

    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (v > 1e-9) - (v < -1e-9)

    for i in range(n):
        a, b = dense[i], dense[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue  # first and last edge share a vertex
            c, d = dense[j], dense[(j + 1) % n]
            if orient(a, b, c) != orient(a, b, d) and orient(c, d, a) != orient(c, d, b):
                raise ValueError('Контурът се пресича сам (проверете точките и радиусите).')
    return pts


def default_profile(kind, angle_deg=None, radius=None, height=None, v=None):
    """A starting outline [[x, y, r, a], ...] (mm, y up, same origin rules as polygon_from_dxf) for the editor / the picture of a tool
    without a stored profile. Punch: `radius` is the tip radius. Die: `radius` is the radius of both SHOULDERS (the edges of the V
    opening); the valley is sharp unless the editor sets a radius. A die is DIE_CHANNEL wide (the press brake's die channel) and `height` tall."""
    if kind == 'punch':
        angle_deg, r, height = angle_deg or 90.0, max(radius or 0.0, 0.2), height or 60.0
        a, off = math.radians(angle_deg) / 2, tip_offset(angle_deg, max(radius or 0.0, 0.2))
        seat = height - off  # catalogue heights are to the theoretical apex
        bx = min(PUNCH_HALF_WIDTH, seat * math.tan(a))
        yb = bx / math.tan(a) - off
        return [[0, -off, r, 0], [bx, yb, 0, 0], [bx, seat, 0, 0], [-bx, seat, 0, 0], [-bx, yb, 0, 0]]
    v = v or 16.0
    rs, a = max(radius or 0.0, 0.0), math.radians(angle_deg or 85.0) / 2
    depth = (v / 2) / math.tan(a)
    w, body = DIE_CHANNEL, height or max(depth + 15, 30)
    return [[-w / 2, 0, 0, 0], [-v / 2, 0, rs, 0], [0, -depth, 0, 0], [v / 2, 0, rs, 0], [w / 2, 0, 0, 0], [w / 2, -body, 0, 0], [-w / 2, -body, 0, 0]]


# ---- outline with real radii ------------------------------------------------------------------------------------
# A stored profile is a list of vertices [x, y, r, a] (legacy [x, y] still reads): r = fillet radius at that corner (0 = sharp; both
# neighbouring edges must be straight), a = radius of the arc replacing the edge from this vertex to the next (0 = straight; a > 0
# bulges to the LEFT of the direction of travel, i.e. into the material when the outline runs counter-clockwise - a concave relief;
# a < 0 bulges out). The radii are data, not a drawing accident: calculations read them from here.

ARC_STEP = math.radians(4)


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _len(v):
    return math.hypot(v[0], v[1])


def _unit(v):
    n = _len(v) or 1.0
    return (v[0] / n, v[1] / n)


def _sample_arc(c, radius, a_from, sweep):
    n = max(2, int(math.ceil(abs(sweep) / ARC_STEP)))
    return [(c[0] + radius * math.cos(a_from + sweep * i / n), c[1] + radius * math.sin(a_from + sweep * i / n)) for i in range(n + 1)]


def _wrap_angle(angle):
    return (angle + math.pi) % (2 * math.pi) - math.pi


def _fillet(prev, v, nxt, r):
    """Tangent points T1 (on the edge to prev), T2 (to next), centre, interior angle and tangent length of a fillet of radius r."""
    u, w = _unit(_sub(prev, v)), _unit(_sub(nxt, v))
    theta = math.acos(max(-1.0, min(1.0, u[0] * w[0] + u[1] * w[1])))
    if theta < math.radians(1) or theta > math.radians(179):
        raise ValueError('Закръгляване върху почти прав или почти нулев ъгъл.')
    t = r / math.tan(theta / 2)
    bis = _unit((u[0] + w[0], u[1] + w[1]))
    d = r / math.sin(theta / 2)
    c = (v[0] + bis[0] * d, v[1] + bis[1] * d)
    return (v[0] + u[0] * t, v[1] + u[1] * t), (v[0] + w[0] * t, v[1] + w[1] * t), c, theta, t


def _edge_arc(a, b, arc):
    """Centre, radius, signed sweep and start angle of the minor arc a -> b with signed radius `arc` (> 0 bulges left of travel)."""
    chord = _len(_sub(b, a))
    r = abs(arc)
    if r < chord / 2 - 1e-9:
        raise ValueError(f'Дъга с радиус R{r:g} е по-малка от половината хорда ({chord / 2:.2f} mm).')
    m = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    n = _unit((-(b[1] - a[1]), b[0] - a[0]))  # left normal
    d = math.sqrt(max(r * r - chord * chord / 4, 0.0))
    s = -1.0 if arc > 0 else 1.0  # bulge left -> the centre is on the right
    c = (m[0] + s * n[0] * d, m[1] + s * n[1] * d)
    a1, a2 = math.atan2(a[1] - c[1], a[0] - c[0]), math.atan2(b[1] - c[1], b[0] - c[0])
    return c, r, _wrap_angle(a2 - a1), a1


def normalize(points):
    """-> [[x, y, r, a], ...] cleaned, counter-clockwise, radii checked. Raises ValueError (Bulgarian)."""
    pts = []
    try:
        for p in points:
            q = [float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0, float(p[3]) if len(p) > 3 else 0.0]
            if not all(math.isfinite(v) for v in q) or abs(q[0]) > 1000 or abs(q[1]) > 1000 or q[2] < 0 or abs(q[3]) > 5000:
                raise ValueError
            pts.append([round(q[0], 2) + 0.0, round(q[1], 2) + 0.0, round(q[2], 3) + 0.0, round(q[3], 3) + 0.0])
    except (TypeError, ValueError, IndexError):
        raise ValueError('Невалидни координати или радиуси (радиусът на закръгляване не може да е отрицателен).')
    clean = []
    for p in pts:
        if not clean or p[:2] != clean[-1][:2]:
            clean.append(p)
    if len(clean) > 1 and clean[0][:2] == clean[-1][:2]:
        clean.pop()
    pts = clean
    n = len(pts)
    if not 3 <= n <= 400:
        raise ValueError('Профилът трябва да има от 3 до 400 точки.')
    area = sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n)) / 2
    if abs(area) < 1:
        raise ValueError('Профилът е вироден (площ под 1 mm²).')
    if area < 0:  # make it counter-clockwise; the arc of an edge stays on that edge but changes side
        pts = [[p[0], p[1], p[2], -pts[(n - 2 - k) % n][3]] for k, p in enumerate(reversed(pts))]
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        if pts[i][3]:
            _edge_arc((a[0], a[1]), (b[0], b[1]), pts[i][3])  # raises when the arc cannot span the edge
    taken = [0.0] * n  # how much of each edge the fillets at its two ends use
    for i, p in enumerate(pts):
        if p[2] > 0:
            prev, nxt = pts[i - 1], pts[(i + 1) % n]
            if prev[3] or p[3]:
                raise ValueError(f'Точка {i + 1}: закръгляване не може да е до дъга - задайте радиуса само на дъгата.')
            t = _fillet(prev[:2], p[:2], nxt[:2], p[2])[4]
            taken[i - 1] += t
            taken[i] += t
    for i in range(n):
        if taken[i] > _len(_sub(pts[(i + 1) % n][:2], pts[i][:2])) + 1e-6:
            raise ValueError(f'Закръгленията при ръб {i + 1}-{(i + 1) % n + 1} не се побират в дължината му.')
    return pts


def outline(points):
    """Dense polygon [[x, y], ...] of a profile - fillets and arcs sampled every few degrees (drawing, clipping, later collisions)."""
    pts = normalize(points)
    n, out = len(pts), []
    for i, p in enumerate(pts):
        prev, nxt = pts[i - 1], pts[(i + 1) % n]
        if p[2] > 0:
            t1, t2, c, _, _ = _fillet(prev[:2], p[:2], nxt[:2], p[2])
            a1, a2 = math.atan2(t1[1] - c[1], t1[0] - c[0]), math.atan2(t2[1] - c[1], t2[0] - c[0])
            out += _sample_arc(c, p[2], a1, _wrap_angle(a2 - a1))
        else:
            out.append((p[0], p[1]))
        if p[3]:
            c, r, sweep, a1 = _edge_arc((p[0], p[1]), (nxt[0], nxt[1]), p[3])
            out += _sample_arc(c, r, a1, sweep)[1:-1]
    return [[round(x, 3), round(y, 3)] for x, y in out]


def radii_report(points):
    """Every radius of the outline for the editor list and for calculations: [{kind, radius, x, y, index}]."""
    pts = normalize(points)
    n, rep = len(pts), []
    for i, p in enumerate(pts):
        if p[2] > 0:
            rep.append({'kind': 'fillet', 'index': i + 1, 'radius': p[2], 'x': p[0], 'y': p[1]})
        if p[3]:
            nxt = pts[(i + 1) % n]
            rep.append({'kind': 'arc', 'index': i + 1, 'radius': abs(p[3]), 'concave': p[3] > 0, 'x': round((p[0] + nxt[0]) / 2, 2), 'y': round((p[1] + nxt[1]) / 2, 2)})
    return rep


def grooves(points):
    """Every V groove of a die outline - a die like M460.r has one on each of its four sides. A groove is a reflex corner (the V valley)
    between two shoulders. -> [{index, v, angle, depth, valley_radius, shoulder_radii, rotation}] sorted by opening; `rotation` is the turn
    (degrees, counter-clockwise) that brings that groove to the top, i.e. the die as it stands on the press brake when that V is used."""
    pts = normalize(points)
    n, out = len(pts), []
    for k in range(n):
        prev, p, nxt = pts[k - 1], pts[k], pts[(k + 1) % n]
        u, w = _sub(p[:2], prev[:2]), _sub(nxt[:2], p[:2])
        if u[0] * w[1] - u[1] * w[0] >= -1e-9:  # a valley is a right turn on a counter-clockwise outline
            continue
        uu, ww = _unit(_sub(prev[:2], p[:2])), _unit(_sub(nxt[:2], p[:2]))
        angle = math.degrees(math.acos(max(-1.0, min(1.0, uu[0] * ww[0] + uu[1] * ww[1]))))
        if not 30 <= angle <= 120 or _len(u) < 2 or _len(w) < 2:
            continue
        mid = ((prev[0] + nxt[0]) / 2, (prev[1] + nxt[1]) / 2)
        axis = _sub(mid, p[:2])
        rot = (90 - math.degrees(math.atan2(axis[1], axis[0])) + 180) % 360 - 180
        out.append({'index': k + 1, 'v': round(_len(_sub(nxt[:2], prev[:2])), 2), 'angle': round(angle, 2), 'depth': round(_len(axis), 2),
                    'valley_radius': p[2] or None, 'shoulder_radii': [prev[2], nxt[2]], 'rotation': round(rot, 1)})
    return sorted(out, key=lambda g: g['v'])


def multi_v_die(size, sides):
    """Outline of a square multi-V die (like M460.r): a size x size block with one V groove on each side. `sides` = four
    (v_opening, angle_deg, shoulder_radius, valley_radius) tuples, clockwise starting with the top side. The V opening is measured
    between the sharp shoulder corners on the face; the groove apex is at depth (v / 2) / tan(angle / 2) before the valley is rounded."""
    h = size / 2
    out = []
    for i, (v, angle, rs, rv) in enumerate(sides):
        depth = (v / 2) / math.tan(math.radians(angle) / 2)
        top = [(-h, h, 0), (-v / 2, h, rs), (0, h - depth, rv), (v / 2, h, rs)]
        c, s_ = math.cos(math.radians(-90 * i)), math.sin(math.radians(-90 * i))  # turn each following side a quarter clockwise
        out += [[round(x * c - y * s_, 3) + 0.0, round(x * s_ + y * c, 3) + 0.0, r, 0] for x, y, r in top]
    return out


def rotated(points, degrees):
    """The outline turned counter-clockwise about the origin (radii kept) - a multi-V die standing on the machine with another V up."""
    c, s_ = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return [[round(p[0] * c - p[1] * s_, 3), round(p[0] * s_ + p[1] * c, 3), p[2], p[3]] for p in normalize(points)]


def derived(points, kind, base_height=None):
    """Numbers read off the outline itself, so the editor can warn when they disagree with the tool's catalogue data."""
    pts = normalize(points)
    dense = outline(pts)
    n, m = len(pts), len(dense)
    xs, ys = [p[0] for p in dense], [p[1] for p in dense]
    res = {'width': round(max(xs) - min(xs), 2), 'height': round(max(ys) - min(ys), 2), 'max_y': round(max(ys), 2), 'min_y': round(min(ys), 2),
           'area': round(abs(sum(dense[i][0] * dense[(i + 1) % m][1] - dense[(i + 1) % m][0] * dense[i][1] for i in range(m))) / 2, 1)}

    def corner(i):  # interior angle at vertex i, degrees
        u, w = _unit(_sub(pts[i - 1][:2], pts[i][:2])), _unit(_sub(pts[(i + 1) % n][:2], pts[i][:2]))
        return round(math.degrees(math.acos(max(-1.0, min(1.0, u[0] * w[0] + u[1] * w[1])))), 2)

    if kind == 'punch':
        i = min(range(n), key=lambda k: pts[k][1])  # the tip corner = the lowest vertex
        res.update(tip_angle=corner(i), tip_radius=pts[i][2] or None)
    else:
        # width of the base zone: sampled over its height (the bottom slice only while no base height is set)
        low = min(ys)
        zone = base_height if base_height else 0.0
        ws = [width_at(dense, low + 0.02 + (zone - 0.04) * i / 4 if zone > 0.04 else low + 0.02) for i in range(5 if zone > 0.04 else 1)]
        widths = [round(w[1] - w[0], 2) for w in ws if w]
        res['base_width'] = min(widths) if widths else None
        res['base_width_max'] = max(widths) if widths else None
        gs = grooves(pts)
        res['grooves'] = gs
        if gs:  # the legacy single-groove numbers: the V that faces up as drawn (or the first one)
            g = min(gs, key=lambda x: abs(x['rotation']))
            res.update(valley_angle=g['angle'], valley_radius=g['valley_radius'], v_opening=g['v'], shoulder_radii=g['shoulder_radii'], groove_depth=g['depth'])
    return res


# Back-gauge finger in the cross-section: a stop face against the end of the flat part, from the sheet's bottom plane up. ponytail: a plain block -
# the real finger (ledge, step, arm) is not drawn yet; send its drawing/dimensions to make this exact.
FINGER = {'thickness': 5.0, 'height': 9.0}


def _chain(start, d, legs, ccw):
    """Points of a part's centre line: from `start` along heading `d`, one leg after the other, turning by each leg's `turn` (+ = curl towards the
    punch: counter-clockwise when heading right, clockwise when heading left)."""
    q = [start]
    for length, turn in legs:
        q.append((q[-1][0] + d[0] * length, q[-1][1] + d[1] * length))
        if turn:
            a = math.radians(turn) * (1 if ccw else -1)
            d = (d[0] * math.cos(a) - d[1] * math.sin(a), d[0] * math.sin(a) + d[1] * math.cos(a))
    return q


# Back-gauge finger (Durma DrmFinger from the controller's gauge file), in the cross-section: level 0 = a 14 mm ledge that sticks out 20 mm in front of
# level 1 (10 mm high), whose front face is the stop wall. The axis X (nose of the ledge) is what the controller is given, not the wall.
LEDGE = {'height': 14.0, 'offset': 20.0, 'wall': 10.0, 'length': 60.0}


def finger_section(wall_x, die_half):
    """(polygon, axis X, 'ledge'|'face'): with the wall far enough from the die the ledge slides under the sheet (its top = the sheet's bottom plane) and the
    sheet end touches level 1's face (axis X = wall - 20). Closer to the bend line the ledge would be in the die: the finger then stands above the die and the
    sheet end touches the front face of level 0 (axis X = wall)."""
    h, off, wl, ln = LEDGE['height'], LEDGE['offset'], LEDGE['wall'], LEDGE['length']
    if wall_x >= die_half + off + 1.0:
        return [(wall_x - off, -h), (wall_x + ln, -h), (wall_x + ln, wl), (wall_x, wl), (wall_x, 0.0), (wall_x - off, 0.0)], wall_x - off, 'ledge'
    return [(wall_x, 0.0), (wall_x + ln, 0.0), (wall_x + ln, h + wl), (wall_x + off, h + wl), (wall_x + off, h), (wall_x, h)], wall_x, 'face'


def _step_geom(punch_pts, die_pts, v, thickness, fold_deg, left, right, overbend=0.0):
    """Layout of one bend at the bottom of the stroke. The sheet rests on the die's shoulders at exact points: its bottom surface is a straight
    line tangent to each shoulder circle (radius R, centre cx = V/2 + R(1 - sin b)/cos b below the top plane, b = half the groove angle); the
    punch tip (radius r) sits on the inner surface, its lowest point r/sin(g) - r above the inner apex. `overbend` = springback allowance:
    the sheet is pressed to fold + overbend and opens back to fold when the punch lifts."""
    gs = grooves(die_pts)
    g = min(gs, key=lambda x: abs(x['v'] - v)) if gs else None
    die = rotated(die_pts, g['rotation']) if g else normalize(die_pts)
    shoulder_y = 0.0
    if g:  # the shoulder plane of the groove that now faces up
        n = len(die)
        shoulder_y = (die[(g['index'] - 2) % n][1] + die[g['index'] % n][1]) / 2
    fold_eff = min(fold_deg + overbend, 179.0)
    gamma = math.radians(180 - fold_eff) / 2             # half the sheet's inner angle
    s, c = math.sin(gamma), math.cos(gamma)
    beta = math.radians(g['angle'] / 2) if g else math.radians(42.5)
    R = (sum(g['shoulder_radii']) / 2 if g else 0.0) or 0.0
    cx = v / 2 + R * (1 - math.sin(beta)) / math.cos(beta)
    y_out = (R - c * cx) / s - R                          # outer apex of the sheet: its bottom line is tangent to the shoulder circle
    contacts = [(-(cx - R * c), -R + R * s), (cx - R * c, -R + R * s)] if R else [(-v / 2, 0.0), (v / 2, 0.0)]
    tip_r = (derived(punch_pts, 'punch').get('tip_radius') or 0.0)
    A = (0.0, y_out + thickness / (2 * s))                # apex on the sheet's centre line
    tip_y = y_out + thickness / s + tip_r / s - tip_r     # lowest point of the punch tip
    dflt = [(max(30.0, v * 0.9), None)]
    left, right = list(left) or dflt, list(right) or dflt
    flat_y = thickness / 2
    dense_die = outline(die)
    wd = width_at(dense_die, max(p[1] for p in dense_die) - 1.0)
    die_half = max(abs(wd[0]), abs(wd[1])) if wd else 30.0
    before_right = _chain((0.0, flat_y), (1.0, 0.0), right, True)
    fx = max(p[0] for p in _sample_part(before_right, thickness))   # the finger touches the WALL of the material (a flange stood up earlier is T/2 further than its centre line)
    finger, axis_x, opt = finger_section(fx, die_half)
    return {'finger': finger, 'axis_x': axis_x, 'finger_option': opt,
            'die': [(x, y - shoulder_y) for x, y in outline(die)],
            'punch': [(x, y + tip_y) for x, y in outline(punch_pts)],
            'clamp': [(x, y + tip_y) for x, y in clamp_polygon(outline(punch_pts))],
            'after': _chain(A, (-s, c), left, False)[::-1][:-1] + _chain(A, (s, c), right, True),   # front end ... apex ... back end
            'before_left': _chain((0.0, flat_y), (-1.0, 0.0), left, False), 'before_right': before_right,
            'right_len': sum(leg for leg, _ in right), 'x_wall': fx, 'flat_y': flat_y, 'contacts': contacts, 'fold_eff': fold_eff,
            'inner_radius': 0.16 * v, 'left': left, 'right': right}


def _inside(pt, poly):
    x, y, inside = pt[0], pt[1], False
    for i in range(len(poly)):
        (x0, y0), (x1, y1) = poly[i - 1], poly[i]
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


def _dist_to_edges(pt, poly):
    best = 1e9
    for i in range(len(poly)):
        (x0, y0), (x1, y1) = poly[i - 1], poly[i]
        dx, dy = x1 - x0, y1 - y0
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((pt[0] - x0) * dx + (pt[1] - y0) * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(pt[0] - x0 - t * dx, pt[1] - y0 - t * dy))
    return best


def _densify(poly, step=1.5):
    out = []
    for i in range(len(poly)):
        (x0, y0), (x1, y1) = poly[i - 1], poly[i]
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        out += [(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n)]
    return out


def _overlap(pts, poly, tol):
    x0, x1 = min(p[0] for p in poly), max(p[0] for p in poly)
    y0, y1 = min(p[1] for p in poly), max(p[1] for p in poly)
    return any(x0 < q[0] < x1 and y0 < q[1] < y1 and _inside(q, poly) and _dist_to_edges(q, poly) > tol for q in pts)


def _sample_part(pts, thickness, step=1.5):
    """Points of the sheet along a centre line: on it and on both surfaces."""
    out = []
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        L = math.hypot(x1 - x0, y1 - y0) or 1.0
        nx, ny = -(y1 - y0) / L, (x1 - x0) / L
        for k in range(int(L / step) + 2):
            f = min(1.0, k * step / L)
            bx, by = x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
            out += [(bx + nx * off, by + ny * off) for off in (-thickness / 2, 0.0, thickness / 2)]
    return out


def _stage_pairs(geo, thickness, tol):
    """Pairs of bodies that overlap at one moment of the stroke."""
    bodies = {'детайл': (_sample_part(geo['after'], thickness), None), 'упор': (_densify(geo['finger']), geo['finger']),
              'поансон': (_densify(geo['punch']), geo['punch']), 'закрепване': (_densify(geo['clamp']), geo['clamp']), 'матрица': (_densify(geo['die']), geo['die'])}
    pairs = [('детайл', 'поансон', tol), ('детайл', 'закрепване', tol), ('детайл', 'матрица', 1.0), ('детайл', 'упор', 0.2),
             ('упор', 'поансон', tol), ('упор', 'закрепване', tol), ('упор', 'матрица', tol), ('поансон', 'матрица', tol), ('закрепване', 'матрица', tol)]
    hits = []
    for a, b, tl in pairs:
        pa, poly_a = bodies[a]
        pb, poly_b = bodies[b]
        if _overlap(pa, poly_b, tl) or (poly_a is not None and _overlap(pb, poly_a, tl)):
            hits.append((a, b))
    return hits


def step_trajectory(punch_pts, die_pts, v, thickness, fold_deg, left=(), right=(), tol=0.4, overbend=0.0, step_deg=6.0):
    """The whole bend, not only its end: the sheet is folded from a few degrees (clamped by the punch) up to fold + overbend, and at every stage
    the punch, its clamp, the die, the part and the back-gauge finger are checked against each other in the cross-section (the profile swings
    round, so a flange or the end of the part can reach the finger or the punch half way). Before the stroke the part rests flat on the die with
    its short end touching the finger face - touching is fine, a flange hanging into the die is not.
    A finger that gets in the way is simply retracted (every machine has that option) - not a collision, unless it sits in the die.
    -> {'hits': ['part - punch', ...], 'retract_deg': angle up to which the finger may stay, or None}. Only the cross-section (3D: stage 3)."""
    final = min(fold_deg + overbend, 179.0)
    stages = sorted({float(a) for a in range(int(step_deg), int(final), int(step_deg))} | {final}) if final > step_deg else [final]
    hits, first_finger = set(), None
    geo = _step_geom(punch_pts, die_pts, v, thickness, final, left, right, 0.0)
    before = _sample_part(geo['before_left'][::-1][:-1] + geo['before_right'], thickness)   # resting on the die before the stroke
    if _overlap(before, geo['die'], 0.6):
        hits.add('детайл преди огъването - матрица')
    for i, a in enumerate(stages):
        g = _step_geom(punch_pts, die_pts, v, thickness, a, left, right, 0.0)
        for x, y in _stage_pairs(g, thickness, tol):
            if 'упор' not in (x, y):
                hits.add(f'{x} - {y}')
            elif 'матрица' in (x, y):
                hits.add(f'{x} - {y}')            # the finger itself stands in the die: retracting does not help
            elif i > 0 and first_finger is None:  # the first stage = the punch clamping the sheet; the finger may be retracted from then on
                first_finger = i
    retract = None if first_finger is None else stages[first_finger - 1]
    return {'hits': sorted(hits), 'retract_deg': retract}


def step_collision(punch_pts, die_pts, v, thickness, fold_deg, left=(), right=(), tol=0.4, overbend=0.0):
    """Collisions over the whole bend (see step_trajectory), the retractable back-gauge finger excluded. -> ['part - punch', ...] (empty = free)."""
    return step_trajectory(punch_pts, die_pts, v, thickness, fold_deg, left, right, tol, overbend)['hits']


def step_svg(punch_pts, die_pts, v, thickness, fold_deg, label='', left=(), right=(), overbend=0.0):
    """Cross-section of one bend with the WHOLE part. Dashed: the part as it is BEFORE this bend (earlier flanges already bent, this bend still
    flat, resting on the die with its short end against the gauge fingers - X is measured there). Solid: the part after the bend. left / right =
    [(leg length, turn)] going away from the bend towards the operator (left, the long part) and the back gauge (right): `turn` = angle to the
    next leg (+ = curling towards the punch), None for the last. A part that runs into the punch is drawn red. y up in the maths, flipped for SVG."""
    geo = _step_geom(punch_pts, die_pts, v, thickness, fold_deg, left, right, overbend)
    traj = step_trajectory(punch_pts, die_pts, v, thickness, fold_deg, left, right, overbend=overbend)
    hits, retract = traj['hits'], traj['retract_deg']
    pts, flat_y = geo['after'], geo['flat_y']
    ghost_l, ghost_r = geo['before_left'], geo['before_right']
    fx = max(p[0] for p in ghost_r)
    allp = geo['die'] + geo['punch'] + geo['clamp'] + pts + ghost_l + ghost_r + geo['finger']
    x0, x1 = min(p[0] for p in allp) - 4, max(p[0] for p in allp) + 6
    y1, y0 = -min(p[1] for p in allp) + 4, -max(p[1] for p in allp) - 4   # svg y is flipped
    path = lambda q: 'M ' + ' L '.join(f'{_f(x)} {_f(-y)}' for x, y in q) + ' Z'
    line = lambda q: 'M ' + ' L '.join(f'{_f(x)} {_f(-y)}' for x, y in q)
    fs = max(3.0, (x1 - x0) / 38)
    col = '#ff3b30' if hits else '#e26a4a'
    gcol = '#ff3b30' if any('преди' in h for h in hits) else '#e26a4a'
    finger = (f'<path d="{path(geo["finger"])}" fill="#4f9d69" stroke="#fff" stroke-width="0.25"/>'
              f'<path d="{line([(0, flat_y - 4 * fs / 3), (fx, flat_y - 4 * fs / 3)])}" stroke="#6fa8d6" stroke-width="{_f(fs / 12)}"/>'
              f'<text x="{_f(fx / 2)}" y="{_f(-(flat_y - 4 * fs / 3) + fs * 1.1)}" font-size="{_f(fs)}" fill="#6fa8d6" text-anchor="middle" font-family="Arial">X {geo["x_wall"]:g}</text>'
              f'<text x="{_f(fx + 2.5)}" y="{_f(-(flat_y + FINGER["height"]) - 1)}" font-size="{_f(fs * 0.8)}" fill="#4f9d69" text-anchor="middle" font-family="Arial">упор</text>')
    from bending.tools import outside_length
    dots = ''.join(f'<circle cx="{_f(x)}" cy="{_f(-y)}" r="{_f(max(0.5, fs / 6))}" fill="#ffd60a" stroke="#14171a" stroke-width="0.15"/>' for x, y in geo['contacts'])
    sizes = ''
    for end_pt, legs in ((pts[0], geo['left']), (pts[-1], geo['right'])):   # outside size of each end flange after the bend
        if legs and legs[-1][1] is None and len(legs) == 1 or (legs and legs[-1][1] is None):
            out = outside_length(legs[-1][0], fold_deg, geo['inner_radius'], thickness)
            sizes += f'<text x="{_f(end_pt[0])}" y="{_f(-end_pt[1] - fs * 0.5)}" font-size="{_f(fs * 0.85)}" fill="#ffd60a" text-anchor="middle" font-family="Arial">{legs[-1][0]:g} → {out:.1f}</text>'
    if retract:   # the finger drawn again, dashed, where it stands after being retracted (moved back), with an arrow
        d = 25.0
        moved = [(x + d, y) for x, y in geo['finger']]
        ymid = thickness + FINGER['height'] / 2
        fingers_back = (f'<path d="{path(moved)}" fill="none" stroke="#4f9d69" stroke-width="{_f(max(0.4, fs / 8))}" stroke-dasharray="1.8 1.2"/>'
                        f'<path d="{line([(fx + FINGER["thickness"] + 1, ymid), (fx + d - 1, ymid)])}" stroke="#4f9d69" stroke-width="{_f(max(0.3, fs / 12))}" stroke-dasharray="1.8 1.2" fill="none"/>'
                        f'<path d="M {_f(fx + d - 1)} {_f(-ymid)} l {_f(-fs * 0.7)} {_f(-fs * 0.35)} l 0 {_f(fs * 0.7)} Z" fill="#4f9d69"/>'
                        f'<text x="{_f(fx + d / 2 + 2.5)}" y="{_f(-ymid - fs * 0.6)}" font-size="{_f(fs * 0.8)}" fill="#4f9d69" text-anchor="middle" font-family="Arial">отдръпнат</text>')
        x1 = max(x1, fx + d + FINGER['thickness'] + 4)
    else:
        fingers_back = ''
    txt = f'<text x="{_f(x0 + 2)}" y="{_f(y0 + fs * 1.3)}" font-size="{_f(fs)}" fill="#9aa5b1" font-family="Arial">{label}</text>' if label else ''
    note = f'<text x="{_f(x0 + 2)}" y="{_f(y0 + fs * 4.1)}" font-size="{_f(fs)}" fill="#ffb340" font-family="Arial">Упорът се отдръпва при ≈{retract:g}° (опция Retract)</text>' if retract else ''
    warn = f'<text x="{_f(x0 + 2)}" y="{_f(y0 + fs * 2.7)}" font-size="{_f(fs)}" fill="#ff3b30" font-family="Arial" font-weight="bold">КОЛИЗИЯ: {"; ".join(hits)}</text>' if hits else ''
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_f(x0)} {_f(y0)} {_f(x1 - x0)} {_f(y1 - y0)}"><title>{label}</title>'
            f'<path d="{path(geo["die"])}" fill="#8fa0b0" stroke="#e0a526" stroke-width="0.4" stroke-linejoin="round"/>'
            f'<path d="{path(geo["clamp"])}" fill="#7d8a97" fill-opacity="0.7" stroke="#e0a526" stroke-opacity="0.5" stroke-width="0.3" stroke-dasharray="1.2 0.8"/>'
            f'<path d="{path(geo["punch"])}" fill="#cfd8e0" stroke="#e0a526" stroke-width="0.4" stroke-linejoin="round"/>'
            f'<path d="{line(ghost_l[::-1][:-1] + ghost_r)}" stroke="{gcol}" stroke-opacity="0.65" stroke-width="{_f(thickness)}" stroke-dasharray="2.5 1.5" stroke-linejoin="round" fill="none"/>'
            f'<path d="{line(pts)}" stroke="{col}" stroke-width="{_f(thickness)}" stroke-linejoin="round" fill="none"/>{dots}{finger}{fingers_back}{sizes}{txt}{warn}{note}</svg>')


def step_frames(punch_pts, die_pts, v, thickness, fold_deg, left=(), right=(), overbend=0.0, n=12, lift=40.0):
    """Animation of one bend for the 3D view (cross-section, mm, y up): punch raised -> down to the sheet -> folded to fold + overbend ->
    springback to fold -> punch raised. The back-gauge finger is moved away from the retract angle on (as in step_trajectory).
    The finger retracts when the punch reaches the sheet (only if it would be in the way later). -> {'die': [...], 'frames': [{'part': centre line, 'punch', 'clamp', 'finger', 'angle'}]}"""
    final = min(fold_deg + overbend, 179.0)
    retract = step_trajectory(punch_pts, die_pts, v, thickness, fold_deg, left, right, overbend=overbend)['retract_deg']
    move = lambda poly, dx=0.0, dy=0.0: [(x + dx, y + dy) for x, y in poly]
    frames = []

    def add(a, dy=0.0, part_a=None, away=None):
        g = _step_geom(punch_pts, die_pts, v, thickness, a, left, right, 0.0)
        gp = g if part_a is None else _step_geom(punch_pts, die_pts, v, thickness, part_a, left, right, 0.0)
        away = retract is not None if away is None else away   # a finger that is in the way is retracted from the moment the punch holds the sheet
        frames.append({'angle': round(a, 2), 'part': gp['after'], 'punch': move(g['punch'], dy=dy), 'clamp': move(g['clamp'], dy=dy),
                       'finger': move(g['finger'], dx=25.0 if away else 0.0)})
        return g

    first = add(0.5)
    frames[0]['part'] = first['before_left'][::-1][:-1] + first['before_right']
    frames[0]['punch'], frames[0]['clamp'] = move(first['punch'], dy=lift), move(first['clamp'], dy=lift)
    frames[0]['finger'] = first['finger']
    for away in ((False, True) if retract is not None else (False,)):   # the punch touches the flat sheet, then the finger moves back while the punch holds it (the part must not shift)
        add(0.5, away=away)
        frames[-1]['part'] = first['before_left'][::-1][:-1] + first['before_right']
    for k in range(1, n + 1):
        add(final * k / n)
    if final > fold_deg + 1e-6:   # the punch lifts, the sheet springs back to the target angle
        add(final, dy=lift / 2, part_a=fold_deg)
    last = add(fold_deg if final > fold_deg + 1e-6 else final, dy=lift)
    return {'die': first['die'], 'frames': frames, 'hits': step_trajectory(punch_pts, die_pts, v, thickness, fold_deg, left, right, overbend=overbend)['hits']}


def step_x(punch_pts, die_pts, v, thickness, fold_deg, left=(), right=()):
    """X of the back gauge for one bend: from the bend line to the wall the finger touches (differs from the developed length of the back leg when
    flanges bent earlier stand up there - the wall of a flange is half a thickness beyond its centre line)."""
    return round(_step_geom(punch_pts, die_pts, v, thickness, fold_deg, left, right)['x_wall'], 1)


def step_axis_x(punch_pts, die_pts, v, thickness, fold_deg, left=(), right=()):
    """(axis X for the controller, 'ledge'|'face') - see finger_section."""
    g = _step_geom(punch_pts, die_pts, v, thickness, fold_deg, left, right)
    return round(g['axis_x'], 1), g['finger_option']
