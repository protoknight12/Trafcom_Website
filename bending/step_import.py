"""STEP (a SolidWorks export) -> parts, sheet-metal check, flat pattern (no Flask; OpenCascade through `cadquery-ocp`).

SolidWorks' own .sldprt/.sldasm are closed formats - only SolidWorks reads them, so the shop exports STEP (AP214) from there.
Sheet metal is recognised from the geometry alone: every face is a plane or a cylinder, and the two sides of the sheet are two sets of faces, each plane
having a parallel partner `t` away and each bend cylinder a coaxial partner of radius +-t. The set of faces of one side (the largest one) is unrolled flat:
planes keep their shape, a bend turns into a strip of the bend allowance (bending.unfold strategy, K-factor by default). Flat pattern layers follow the
/flashing convention: CUT + BEND_UP_<angle> / BEND_DOWN_<angle> (UP = the sheet folds towards the viewer), which bending.dxf_bends reads back.
Not supported (reported as a reason, never guessed): cones/splines, two bends in a row, a plane straight into a plane, closed boxes (the unfold needs a tree)."""
import math

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.GProp import GProp_GProps
from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Cylinder, GeomAbs_Line, GeomAbs_Plane
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.IFSelect import IFSelect_RetDone
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDF import TDF_Label
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.collections import Sequence_TDF_Label
from OCP.gp import gp_Pnt, gp_Vec

from bending.unfold import k_factor

MAX_T = 25.0   # thicker than this is a plate/bar, not sheet metal


# ---------- vectors (plain tuples; OCP types only at the edges of this module) ----------
def _sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def _add(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def _mul(a, k): return (a[0] * k, a[1] * k, a[2] * k)
def _dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def _cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
def _len(a): return math.sqrt(_dot(a, a))
def _unit(a):
    n = _len(a)
    return (a[0] / n, a[1] / n, a[2] / n)
def _xyz(p): return (p.X(), p.Y(), p.Z())


# ---------- reading ----------
def _label_name(label):
    a = TDataStd_Name()
    name = a.Get().ToExtString() if label.FindAttribute(TDataStd_Name.GetID_s(), a) else ''
    try:   # some exporters write UTF-8 bytes as Latin-1 characters ('ÐÐ¾...' for Cyrillic): take them back
        return name.encode('latin-1').decode('utf-8') if any(0x80 <= ord(ch) < 0x100 for ch in name) else name
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def read_parts(path):
    """STEP file -> [{'name', 'qty', 'shape'}]: every distinct part once, qty = how many times the assembly uses it (nested sub-assemblies multiply).
    A plain single-part file gives one entry. A part with several solids gives one entry per solid ('name 2', ...)."""
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString('XmlOcaf'))
    app.NewDocument(TCollection_ExtendedString('XmlOcaf'), doc)
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    if reader.ReadFile(path) != IFSelect_RetDone or not reader.Transfer(doc):
        raise ValueError('Файлът не е валиден STEP.')
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    found = {}   # label tag path -> entry (a part used twice is one entry)

    def walk(label, mult):
        if tool.IsAssembly_s(label):
            comps = Sequence_TDF_Label()
            tool.GetComponents_s(label, comps)
            for comp in comps:
                ref = TDF_Label()
                if tool.GetReferredShape_s(comp, ref):
                    walk(ref, mult)
            return
        key = ref_key(label)
        if key in found:
            found[key]['qty'] += mult
        else:
            found[key] = {'name': _label_name(label) or f'Част {len(found) + 1}', 'qty': mult, 'shape': tool.GetShape_s(label)}

    def ref_key(label):
        tags, node = [], label
        while not node.IsNull():
            tags.append(node.Tag())
            node = node.Father()
        return tuple(tags)

    free = Sequence_TDF_Label()
    tool.GetFreeShapes(free)
    for label in free:
        walk(label, 1)
    out = []
    for e in found.values():
        solids = []
        exp = TopExp_Explorer(e['shape'], TopAbs_SOLID)
        while exp.More():
            solids.append(TopoDS.Solid(exp.Current()))
            exp.Next()
        for i, s in enumerate(solids):
            out.append({'name': e['name'] if i == 0 else f"{e['name']} {i + 1}", 'qty': e['qty'], 'shape': s})
    if not out:
        raise ValueError('В STEP файла няма твърди тела.')
    return out


# ---------- face analysis ----------
class _Face:
    def __init__(self, shape):
        self.shape = shape
        self.surf = BRepAdaptor_Surface(shape)
        kind = self.surf.GetType()
        self.kind = 'plane' if kind == GeomAbs_Plane else 'cyl' if kind == GeomAbs_Cylinder else 'other'
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(shape, props)
        self.area = props.Mass()
        self.center = _xyz(props.CentreOfMass())
        u0, u1, v0, v1 = self.surf.FirstUParameter(), self.surf.LastUParameter(), self.surf.FirstVParameter(), self.surf.LastVParameter()
        self.urange = (u0, u1)
        self.n = self.normal_at((u0 + u1) / 2, (v0 + v1) / 2)
        if self.kind == 'plane':
            self.origin = _xyz(self.surf.Plane().Location())
        elif self.kind == 'cyl':
            cyl = self.surf.Cylinder()
            self.c, self.axis, self.radius = _xyz(cyl.Location()), _unit(_xyz(cyl.Axis().Direction())), cyl.Radius()
            self.xdir, self.ydir = _unit(_xyz(cyl.XAxis().Direction())), _unit(_xyz(cyl.YAxis().Direction()))
            mid = self._point((u0 + u1) / 2, (v0 + v1) / 2)
            radial = _sub(mid, _add(self.c, _mul(self.axis, _dot(_sub(mid, self.c), self.axis))))
            self.convex = _dot(self.n, radial) > 0   # outward normal away from the axis: the outer face of a bend

    def _point(self, u, v):
        return _xyz(self.surf.Value(u, v))

    def normal_at(self, u, v):
        p, vec = gp_Pnt(), gp_Vec()
        BRepGProp_Face(self.shape).Normal(u, v, p, vec)
        return _unit(_xyz(vec))

    def u_of(self, p):
        d = _sub(p, self.c)
        return math.atan2(_dot(d, self.ydir), _dot(d, self.xdir))


def _faces_of(solid):
    out, exp = [], TopExp_Explorer(solid, TopAbs_FACE)
    while exp.More():
        out.append(_Face(TopoDS.Face(exp.Current())))
        exp.Next()
    return out


def _volume_area(solid):
    v, a = GProp_GProps(), GProp_GProps()
    BRepGProp.VolumeProperties_s(solid, v)
    BRepGProp.SurfaceProperties_s(solid, a)
    return abs(v.Mass()), a.Mass()


def _thickness(faces, vol, area):
    """The sheet thickness: the distance of antiparallel plane pairs (area-weighted) nearest to what volume/area suggests (2V/A is a bit below t)."""
    t0 = 2 * vol / area
    votes = {}
    planes = [f for f in faces if f.kind == 'plane']
    for i, f in enumerate(planes):
        for g in planes[i + 1:]:
            if _dot(f.n, g.n) < -0.999:
                d = abs(_dot(_sub(g.origin, f.origin), f.n))
                if 0.9 * t0 <= d <= 2.0 * t0 and d <= MAX_T:
                    key = round(d, 2)
                    votes[key] = votes.get(key, 0.0) + f.area + g.area
    return max(votes, key=votes.get) if votes else None


def _partnered(f, faces, t):
    tol = 0.02 * t + 0.01
    for g in faces:
        if g is f or f.kind != g.kind:
            continue
        if f.kind == 'plane' and _dot(f.n, g.n) < -0.999 and abs(abs(_dot(_sub(g.origin, f.origin), f.n)) - t) < tol:
            return True
        if f.kind == 'cyl' and abs(_dot(f.axis, g.axis)) > 0.999 and _len(_cross(_sub(g.c, f.c), f.axis)) < tol and abs(abs(f.radius - g.radius) - t) < tol:
            return True
    return False


def analyze(solid):
    """-> {'sheet': bool, 'why': reason when not, 'thickness', 'bends', 'volume', 'bbox': (x, y, z)}. Cheap; the flat pattern is a separate call."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    vol, area = _volume_area(solid)
    box = Bnd_Box()
    BRepBndLib.Add_s(solid, box)
    lo, hi = box.CornerMin(), box.CornerMax()
    x0, y0, z0, x1, y1, z1 = lo.X(), lo.Y(), lo.Z(), hi.X(), hi.Y(), hi.Z()
    info = {'sheet': False, 'why': None, 'thickness': None, 'bends': 0, 'volume': vol, 'bbox': (round(x1 - x0, 1), round(y1 - y0, 1), round(z1 - z0, 1))}
    faces = _faces_of(solid)
    if any(f.kind == 'other' for f in faces):
        info['why'] = 'Има повърхнини, различни от равнина и цилиндър.'
        return info
    t = _thickness(faces, vol, area)
    if not t:
        info['why'] = 'Няма успоредни стени на разстояние на дебелина на ламарина.'
        return info
    side = [f for f in faces if _partnered(f, faces, t)]
    top = _top_side(side)
    if not top:
        info['why'] = 'Не може да се отдели страна на листа.'
        return info
    bottom = [f for f in side if f not in top]
    mid = (sum(f.area for f in top) + sum(f.area for f in bottom)) / 2
    if not 0.93 < vol / (t * mid) < 1.07:
        info['why'] = 'Обемът не отговаря на лист с постоянна дебелина.'
        return info
    info.update(sheet=True, thickness=t, bends=sum(1 for f in top if f.kind == 'cyl'))
    return info


def _top_side(side):
    """The connected set (through shared edges) of the faces that holds the largest face."""
    if not side:
        return []
    shared = {}
    for f in side:
        exp = TopExp_Explorer(f.shape, TopAbs_EDGE)
        while exp.More():
            shared.setdefault(hash(exp.Current()), []).append(f)
            exp.Next()
    nbr = {id(f): [] for f in side}
    for fs in shared.values():
        for f in fs:
            nbr[id(f)] += [g for g in fs if g is not f]
    start = max(side, key=lambda f: f.area)
    seen, todo = {id(start): start}, [start]
    while todo:
        for g in nbr[id(todo.pop())]:
            if id(g) not in seen:
                seen[id(g)] = g
                todo.append(g)
    return list(seen.values())


# ---------- flat pattern ----------
class _Map:
    """3D -> 2D for one planar face: P2 = Q2 + ((p-q).a) a2 + ((p-q).d) d2, with a, d in the face's plane (an unrolled rigid motion of it)."""
    def __init__(self, q, a, d, q2, a2, d2):
        self.q, self.a, self.d, self.q2, self.a2, self.d2 = q, a, d, q2, a2, d2

    def __call__(self, p):
        v = _sub(p, self.q)
        x, y = _dot(v, self.a), _dot(v, self.d)
        return (self.q2[0] + x * self.a2[0] + y * self.d2[0], self.q2[1] + x * self.a2[1] + y * self.d2[1])

    def vec(self, v):
        x, y = _dot(v, self.a), _dot(v, self.d)
        return (x * self.a2[0] + y * self.d2[0], x * self.a2[1] + y * self.d2[1])


class _CylMap:
    """3D -> 2D for a bend face: along the axis as in the plane it started from, around the axis as the bend allowance `ba` (rolled out from the start edge)."""
    def __init__(self, face, start_map, q, dir2, ba):
        self.f, self.start, self.q, self.dir2, self.ba = face, start_map, q, dir2, ba
        u0, u1 = face.urange
        self.theta = u1 - u0
        self.u_start = face.u_of(q)
        mid = (u0 + u1) / 2 - self.u_start
        self.sign = 1.0 if math.atan2(math.sin(mid), math.cos(mid)) >= 0 else -1.0

    def angle_at(self, p):
        th = ((self.f.u_of(p) - self.u_start) * self.sign) % (2 * math.pi)
        return th - 2 * math.pi if th > self.theta + (2 * math.pi - self.theta) / 2 else th

    def __call__(self, p):
        f = self.f
        base = self.start(_add(self.q, _mul(f.axis, _dot(_sub(p, self.q), f.axis))))
        k = self.angle_at(p) / self.theta * self.ba
        return (base[0] + self.dir2[0] * k, base[1] + self.dir2[1] * k)


def _norm2(v):
    k = math.hypot(*v)
    return (v[0] / k, v[1] / k)


def _edge_data(edge, deflection=0.02):
    """-> (kind, data): ('line', (p, q)) | ('circle', (center, radius, p_first, p_mid, p_last, closed)) | ('poly', [points]); 3D tuples."""
    c = BRepAdaptor_Curve(edge)
    a, b = c.FirstParameter(), c.LastParameter()
    t = c.GetType()
    if t == GeomAbs_Line:
        return 'line', (_xyz(c.Value(a)), _xyz(c.Value(b)))
    if t == GeomAbs_Circle:
        circ = c.Circle()
        return 'circle', (_xyz(circ.Location()), circ.Radius(), _xyz(c.Value(a)), _xyz(c.Value((a + b) / 2)), _xyz(c.Value(b)), abs((b - a) - 2 * math.pi) < 1e-6)
    pts = GCPnts_QuasiUniformDeflection(c, deflection)
    return 'poly', [_xyz(pts.Value(i)) for i in range(1, pts.NbPoints() + 1)]


def _circle_2d(p0, pm, p1):
    """Centre and radius of the circle through three 2D points; None when collinear."""
    ax, ay = p0
    bx, by = pm
    cx, cy = p1
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return (ux, uy), math.hypot(ax - ux, ay - uy)


def flat_pattern(solid, strategy=None):
    """Unroll the largest side of a sheet-metal solid. -> {'thickness', 'cut': [entity], 'bends': [{'angle', 'dir', 'p0', 'p1', 'radius', 'allowance'}], 'size': (w, h)};
    entities: ('line', p, q) | ('circle', c, r) | ('arc', c, r, start_deg, end_deg), 2D mm shifted so the part starts at (0, 0). ValueError with the reason otherwise."""
    strategy = strategy or k_factor(0.44)
    info = analyze(solid)
    if not info['sheet']:
        raise ValueError(info['why'] or 'Не е ламарина.')
    t = info['thickness']
    faces = _faces_of(solid)
    top = _top_side([f for f in faces if _partnered(f, faces, t)])
    by_edge = {}
    for f in top:
        exp = TopExp_Explorer(f.shape, TopAbs_EDGE)
        while exp.More():
            by_edge.setdefault(hash(exp.Current()), []).append((f, TopoDS.Edge(exp.Current())))
            exp.Next()
    root = max((f for f in top if f.kind == 'plane'), key=lambda f: f.area, default=None)
    if root is None:
        raise ValueError('Няма равна страна, от която да започне развивката.')
    a0 = _unit(_cross(root.n, (1, 0, 0) if abs(root.n[0]) < 0.9 else (0, 1, 0)))
    maps = {id(root): _Map(root.center, a0, _cross(root.n, a0), (0.0, 0.0), (1.0, 0.0), (0.0, 1.0))}
    bends, todo = [], [root]
    while todo:
        f = todo.pop()
        for pairs in by_edge.values():
            if len(pairs) != 2 or not any(x is f for x, _ in pairs):
                continue
            g, edge = next((x, e) for x, e in pairs if x is not f)
            if id(g) in maps:
                continue
            if f.kind == 'plane' and g.kind == 'cyl':
                maps[id(g)] = _start_bend(f, maps[id(f)], g, edge, t, strategy, bends)
            elif f.kind == 'cyl' and g.kind == 'plane':
                maps[id(g)] = _continue_flat(maps[id(f)], g, edge)
            else:
                raise ValueError('Две огъвки една след друга или плоскост направо в плоскост - не се поддържа.')
            todo.append(g)
    if len(maps) != len(top):
        raise ValueError('Не всички стени на листа са свързани в дърво (затворена кутия?).')
    cut = []
    for pairs in by_edge.values():
        if len(pairs) == 1:   # a boundary of the side: borders an edge face (outline, hole, slot) - a cut
            f, edge = pairs[0]
            cut += _cut_entities(edge, maps[id(f)])
    allpts = [p for e in cut for p in _entity_pts(e)] + [p for b in bends for p in (b['p0'], b['p1'])]
    x0, y0 = min(p[0] for p in allpts), min(p[1] for p in allpts)
    x1, y1 = max(p[0] for p in allpts), max(p[1] for p in allpts)
    sh = lambda p: (p[0] - x0, p[1] - y0)
    for b in bends:
        b['p0'], b['p1'] = sh(b['p0']), sh(b['p1'])
    return {'thickness': t, 'cut': [_shift(e, sh) for e in cut], 'bends': bends, 'size': (x1 - x0, y1 - y0)}


def _start_bend(plane, pmap, cyl, edge, t, strategy, bends):
    """The bend face `cyl` reached from `plane` through the tangent edge: its rolled-out map; also records the bend line."""
    c = BRepAdaptor_Curve(edge)
    p0, p1 = _xyz(c.Value(c.FirstParameter())), _xyz(c.Value(c.LastParameter()))
    q = _mul(_add(p0, p1), 0.5)
    # which way the bend runs away from the plane: a point of the bend face a little way in, projected into the plane
    u0, u1 = cyl.urange
    v0, v1 = cyl.surf.FirstVParameter(), cyl.surf.LastVParameter()
    here = cyl.u_of(q)
    mid = (u0 + u1) / 2 - here
    step = 0.05 * (u1 - u0) * (1 if math.atan2(math.sin(mid), math.cos(mid)) >= 0 else -1)
    near = _xyz(cyl.surf.Value(here + step, (v0 + v1) / 2))
    away = _sub(near, q)
    away = _sub(_sub(away, _mul(plane.n, _dot(away, plane.n))), _mul(cyl.axis, _dot(away, cyl.axis)))
    dir2 = _norm2(pmap.vec(_unit(away)))
    r_in = cyl.radius - t if cyl.convex else cyl.radius
    angle = math.degrees(u1 - u0)
    ba = strategy(angle, r_in, t, (r_in + t) * math.tan(math.radians(angle) / 2))
    m = _CylMap(cyl, pmap, q, dir2, ba)
    a, b = pmap(p0), pmap(p1)
    bends.append({'angle': angle, 'dir': 'down' if cyl.convex else 'up', 'radius': r_in, 'allowance': ba,
                  'p0': (a[0] + dir2[0] * ba / 2, a[1] + dir2[1] * ba / 2), 'p1': (b[0] + dir2[0] * ba / 2, b[1] + dir2[1] * ba / 2)})
    return m


def _continue_flat(cmap, plane, edge):
    """The plane that continues after a bend: placed at the far end of the bend strip, its edge on the strip's end line."""
    c = BRepAdaptor_Curve(edge)
    q = _mul(_add(_xyz(c.Value(c.FirstParameter())), _xyz(c.Value(c.LastParameter()))), 0.5)
    axis = cmap.f.axis
    d = _sub(plane.center, q)
    d = _unit(_sub(_sub(d, _mul(axis, _dot(d, axis))), _mul(plane.n, _dot(d, plane.n))))   # into the plane, away from the bend
    s1, s0 = cmap.start(_add(q, axis)), cmap.start(q)
    a2 = _norm2((s1[0] - s0[0], s1[1] - s0[1]))
    return _Map(q, axis, d, cmap(q), a2, cmap.dir2)


def _cut_entities(edge, m):
    kind, data = _edge_data(edge)
    if kind == 'line':
        return [('line', m(data[0]), m(data[1]))]
    if kind == 'circle' and isinstance(m, _Map):
        center, r, pf, pm, pl, closed = data
        if closed:
            return [('circle', m(center), r)]
        c2 = _circle_2d(m(pf), m(pm), m(pl))
        if c2:
            (cx, cy), rr = c2
            ang = lambda p: math.degrees(math.atan2(p[1] - cy, p[0] - cx)) % 360
            a0, am, a1 = ang(m(pf)), ang(m(pm)), ang(m(pl))
            return [('arc', (cx, cy), rr, a0, a1) if (am - a0) % 360 < (a1 - a0) % 360 else ('arc', (cx, cy), rr, a1, a0)]
    pts = [m(p) for p in (data if kind == 'poly' else _sample(edge))]
    chord = math.dist(pts[0], pts[-1])
    if chord and max(abs((pts[-1][0] - pts[0][0]) * (p[1] - pts[0][1]) - (pts[-1][1] - pts[0][1]) * (p[0] - pts[0][0])) / chord for p in pts) < 0.01:
        return [('line', pts[0], pts[-1])]   # a straight edge once rolled out (the side of a bend strip)
    return [('line', a, b) for a, b in zip(pts, pts[1:])]


def _sample(edge, n=24):
    c = BRepAdaptor_Curve(edge)
    a, b = c.FirstParameter(), c.LastParameter()
    return [_xyz(c.Value(a + (b - a) * i / n)) for i in range(n + 1)]


def _entity_pts(e):
    if e[0] == 'line':
        return [e[1], e[2]]
    cx, cy = e[1]
    r = e[2]
    if e[0] == 'circle':
        return [(cx - r, cy - r), (cx + r, cy + r)]
    span = (e[4] - e[3]) % 360
    return [(cx + r * math.cos(math.radians(e[3] + span * i / 8)), cy + r * math.sin(math.radians(e[3] + span * i / 8))) for i in range(9)]


def _shift(e, sh):
    if e[0] == 'line':
        return ('line', sh(e[1]), sh(e[2]))
    return (e[0], sh(e[1])) + tuple(e[2:])


def write_dxf(flat, path):
    """Flat pattern -> DXF (mm): layer CUT + dashed BEND_UP_<angle> / BEND_DOWN_<angle> lines, the /flashing convention."""
    import ezdxf
    doc = ezdxf.new('R2000')
    doc.units = ezdxf.units.MM
    doc.linetypes.add('DASHED', pattern=[9.0, 6.0, -3.0], description='Dashed')
    doc.layers.add('CUT', color=7)
    msp = doc.modelspace()
    for e in flat['cut']:
        at = {'layer': 'CUT'}
        if e[0] == 'line':
            msp.add_line(e[1], e[2], dxfattribs=at)
        elif e[0] == 'circle':
            msp.add_circle(e[1], e[2], dxfattribs=at)
        else:
            msp.add_arc(e[1], e[2], e[3], e[4], dxfattribs=at)
    for b in flat['bends']:
        layer = f"BEND_{b['dir'].upper()}_{int(round(b['angle']))}"
        if layer not in doc.layers:
            doc.layers.add(layer, color=3 if b['dir'] == 'up' else 5, linetype='DASHED')
        msp.add_line(b['p0'], b['p1'], dxfattribs={'layer': layer})
    doc.saveas(path)


# ---------- storing and showing a part ----------
def write_part_step(shape, path):
    """One part -> its own STEP file (what is kept with the Detail)."""
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    if w.Write(path) != IFSelect_RetDone:
        raise ValueError('STEP файлът не може да се запише.')


def mesh(path, max_triangles=300000):
    """STEP file -> {'positions': [x, y, z, ...], 'indices': [i, ...], 'edges': [[x, y, z, ...], ...], 'bbox': [[min], [max]]} for the browser's three.js (mm).
    Read as a whole (assembly instances keep their places). The tessellation tolerance follows the part's size, so a bracket is smooth and a hall stays light."""
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.Bnd import Bnd_Box
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location
    r = STEPControl_Reader()
    if r.ReadFile(path) != IFSelect_RetDone:
        raise ValueError('Файлът не е валиден STEP.')
    r.TransferRoots()
    shape = r.OneShape()
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    lo, hi = box.CornerMin(), box.CornerMax()
    diag = math.dist((lo.X(), lo.Y(), lo.Z()), (hi.X(), hi.Y(), hi.Z()))
    defl = max(0.05, diag / 1500)
    while True:
        BRepMesh_IncrementalMesh(shape, defl, False, 0.4)
        pos, idx = [], []
        exp = TopExp_Explorer(shape, TopAbs_FACE)
        while exp.More():
            face = TopoDS.Face(exp.Current())
            loc = TopLoc_Location()
            tri = BRep_Tool.Triangulation_s(face, loc)
            if tri is not None:
                base, trsf = len(pos) // 3, loc.Transformation()
                for i in range(1, tri.NbNodes() + 1):
                    p = tri.Node(i).Transformed(trsf)
                    pos += [round(p.X(), 3), round(p.Y(), 3), round(p.Z(), 3)]
                flip = face.Orientation() == TopAbs_REVERSED
                for i in range(1, tri.NbTriangles() + 1):
                    a, b, c = tri.Triangle(i).Get()
                    idx += [base + a - 1, base + c - 1, base + b - 1] if flip else [base + a - 1, base + b - 1, base + c - 1]
            exp.Next()
        if len(idx) // 3 <= max_triangles or defl > diag:
            break
        defl *= 2
    edges, seen = [], set()
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp.More():
        e = exp.Current()
        if hash(e) not in seen:
            seen.add(hash(e))
            pts = _sample(TopoDS.Edge(e), 2) if BRepAdaptor_Curve(TopoDS.Edge(e)).GetType() == GeomAbs_Line else _sample(TopoDS.Edge(e), 24)
            edges.append([round(v, 3) for p in pts for v in p])
        exp.Next()
    return {'positions': pos, 'indices': idx, 'edges': edges, 'bbox': [[lo.X(), lo.Y(), lo.Z()], [hi.X(), hi.Y(), hi.Z()]]}
