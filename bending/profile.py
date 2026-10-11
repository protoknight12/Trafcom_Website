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


def tool_svg(kind, angle_deg, radius, height, v, code='', profile=None):
    """Picture of a tool: its stored outline, or - for a tool without one yet - a starting outline built from its numbers."""
    return polygon_svg(profile or default_profile(kind, angle_deg, radius, height, v), kind, code)


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


def polygon_svg(points, kind, code=''):
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
    label = f'<text x="{_f(x0 + 2)}" y="{_f(y1 - 1)}" font-size="3.2" fill="#9aa5b1" font-family="Arial">{GRID_MM:g} mm</text>' if grid else ''
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_f(x0)} {_f(y0)} {_f(x1 - x0)} {_f(y1 - y0)}"><title>{code}</title>{grid}{clamp}'
            f'<path d="{path}" fill="#cfd8e0" fill-opacity="0.95" stroke="#e0a526" stroke-width="0.5" stroke-linejoin="round"/>{label}</svg>')


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
    """A starting outline [[x, y, r, a], ...] (mm, y up, same origin rules as polygon_from_dxf) for the editor when a tool has none yet.
    The tip / valley radius is the tool's own radius; shoulder radii are unknown here and start sharp (set them in the editor)."""
    if kind == 'punch':
        angle_deg, r, height = angle_deg or 90.0, max(radius or 0.0, 0.2), height or 60.0
        a, off = math.radians(angle_deg) / 2, tip_offset(angle_deg, max(radius or 0.0, 0.2))
        seat = height - off  # catalogue heights are to the theoretical apex
        bx = min(PUNCH_HALF_WIDTH, seat * math.tan(a))
        yb = bx / math.tan(a) - off
        return [[0, -off, r, 0], [bx, yb, 0, 0], [bx, seat, 0, 0], [-bx, seat, 0, 0], [-bx, yb, 0, 0]]
    v = v or 16.0
    r, a = max(radius or 0.0, 0.2), math.radians(angle_deg or 85.0) / 2
    depth = (v / 2) / math.tan(a)
    w, body = v + 30, max(depth + 15, 30)
    return [[-w / 2, 0, 0, 0], [-v / 2, 0, 0, 0], [0, -depth, r, 0], [v / 2, 0, 0, 0], [w / 2, 0, 0, 0], [w / 2, -body, 0, 0], [-w / 2, -body, 0, 0]]


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
            pts.append([round(q[0], 2), round(q[1], 2), round(q[2], 3), round(q[3], 3)])
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


def derived(points, kind):
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
        valleys = [k for k in range(n) if pts[k - 1][1] > pts[k][1] and pts[(k + 1) % n][1] > pts[k][1]]
        if valleys:
            k = max(valleys, key=lambda j: min(pts[j - 1][1], pts[(j + 1) % n][1]) - pts[j][1])  # the deepest groove
            a, b = pts[k - 1], pts[(k + 1) % n]
            res.update(valley_angle=corner(k), valley_radius=pts[k][2] or None, v_opening=round(abs(b[0] - a[0]), 2),
                       shoulder_radii=[q[2] for q in (a, b)], groove_depth=round(min(a[1], b[1]) - pts[k][1], 2))
    return res
