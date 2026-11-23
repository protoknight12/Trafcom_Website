"""
FSCUT / CypCutE nesting files ("разкрой") - read-only parser, no Flask.

Two formats, same content:
  .lxd  (old)  one plain XML file <LXDDocument>; geometry in XML.
  .lxds (new)  ZIP of XML parts; coordinates in the binary Geometry2D/data.bin, addressed by
               Shapes2D/content.xml GeoAddr. See docs/FSCUT_FORMAT.md for the layout.

parse(path) -> dict (see _empty()). Shapes use the same dicts as analyze_dxf_geometry() /
static/js/dxf_viewer.js: line {x1,y1,x2,y2}, circle {cx,cy,r}, arc {cx,cy,r,start_angle,end_angle} (degrees, CCW).
Nothing here writes or executes anything from the file; malformed input raises ValueError.
"""
import math
import struct
import zipfile
import xml.etree.ElementTree as ET

GAS = {1: 'air', 2: 'oxygen', 3: 'nitrogen', 4: 'air', 5: 'oxygen', 6: 'nitrogen'}   # CypCut GasType -> CUT_GASES key
# CypCut's material names are Chinese in our files (Chinese UI language of the machine PC)
MATERIAL_NAMES = {'碳钢': 'Черна стомана', '不锈钢': 'Неръждаема стомана', '铝': 'Алуминий', '铝板': 'Алуминий',
                  '黄铜': 'Месинг', '铜': 'Мед', '紫铜': 'Мед', '镀锌板': 'Поцинкована ламарина',
                  '镀锌': 'Поцинкована ламарина', '钛': 'Титан'}
MAX_SHAPES = 200_000          # shapes + entity visits per file (nested Inserts multiply, so counted everywhere)


def material_name(name):
    return MATERIAL_NAMES.get((name or '').strip(), name)
BEZIER_STEPS = 8
POINT_TAG = b'\x10\x00\x02\x00'


def _fromstring(data):
    """CypCut never writes a DTD; refusing one rules out entity-expansion tricks (stdlib ET never fetches external
    entities) without a new dependency."""
    if b'<!DOCTYPE' in data or b'<!ENTITY' in data:
        raise ValueError('DTD not allowed')
    return ET.fromstring(data)


def _spend(budget, n):
    """One running count per file of entity visits + shapes produced; ValueError past MAX_SHAPES."""
    budget[0] += n
    if budget[0] > MAX_SHAPES:
        raise ValueError('too many shapes')


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _xy(e):
    return (_f(e.get('X')), _f(e.get('Y'))) if e is not None else (0.0, 0.0)


# ---------- shapes ----------

def _bulge_shape(p1, p2, bulge):
    """One polyline segment: a line, or (bulge != 0) the DXF bulge arc from p1 to p2."""
    if abs(bulge) < 1e-9:
        return {'type': 'line', 'x1': p1[0], 'y1': p1[1], 'x2': p2[0], 'y2': p2[1]}
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    chord = math.hypot(dx, dy)
    if chord < 1e-9:
        return None
    theta = 4 * math.atan(bulge)                       # signed included angle
    r = chord / (2 * math.sin(abs(theta) / 2))
    mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
    d = r * math.cos(theta / 2)                        # centre distance from the chord midpoint
    # bulge > 0: CCW arc, centre to the left of p1->p2
    sign = 1 if bulge > 0 else -1
    cx, cy = mx - sign * d * dy / chord, my + sign * d * dx / chord
    a1 = math.degrees(math.atan2(p1[1] - cy, p1[0] - cx))
    a2 = math.degrees(math.atan2(p2[1] - cy, p2[0] - cx))
    start, end = (a1, a2) if bulge > 0 else (a2, a1)
    return {'type': 'arc', 'cx': cx, 'cy': cy, 'r': abs(r), 'start_angle': start % 360, 'end_angle': end % 360}


def _poly_shapes(points, closed):
    """points: [(x, y, bulge)]."""
    out = []
    n = len(points)
    for i in range(n if closed else n - 1):
        x1, y1, b = points[i]
        x2, y2, _ = points[(i + 1) % n]
        s = _bulge_shape((x1, y1), (x2, y2), b)
        if s:
            out.append(s)
    return out


def _bezier_shapes(pts):
    """Cubic segments of 4 control points each, flattened into lines."""
    out = []
    for k in range(0, len(pts) - 3, 4):
        p0, p1, p2, p3 = pts[k:k + 4]
        prev = p0
        for i in range(1, BEZIER_STEPS + 1):
            t = i / BEZIER_STEPS
            u = 1 - t
            x = u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0]
            y = u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]
            out.append({'type': 'line', 'x1': prev[0], 'y1': prev[1], 'x2': x, 'y2': y})
            prev = (x, y)
    return out


def _transform(shapes, base, insert, angle_rad, sx=1.0, sy=1.0):
    """Block -> placed: (p - base) * scale, rotated, + insert."""
    if not any((base != (0.0, 0.0), insert != (0.0, 0.0), angle_rad, sx != 1.0, sy != 1.0)):
        return shapes
    c, s = math.cos(angle_rad), math.sin(angle_rad)

    def tp(x, y):
        x, y = (x - base[0]) * sx, (y - base[1]) * sy
        return x * c - y * s + insert[0], x * s + y * c + insert[1]
    deg = math.degrees(angle_rad)
    mirrored = sx * sy < 0
    out = []
    for sh in shapes:
        sh = dict(sh)
        if sh['type'] == 'line':
            sh['x1'], sh['y1'] = tp(sh['x1'], sh['y1'])
            sh['x2'], sh['y2'] = tp(sh['x2'], sh['y2'])
        else:
            sh['cx'], sh['cy'] = tp(sh['cx'], sh['cy'])
            sh['r'] *= abs(sx)
            if sh['type'] == 'arc':
                a, b = sh['start_angle'], sh['end_angle']
                if mirrored:
                    a, b = 180 - b, 180 - a
                sh['start_angle'], sh['end_angle'] = (a + deg) % 360, (b + deg) % 360
        out.append(sh)
    return out


def shape_length(sh):
    if sh['type'] == 'line':
        return math.hypot(sh['x2'] - sh['x1'], sh['y2'] - sh['y1'])
    if sh['type'] == 'circle':
        return 2 * math.pi * sh['r']
    sweep = (sh['end_angle'] - sh['start_angle']) % 360 or 360
    return math.radians(sweep) * sh['r']


def _ends(sh):
    if sh['type'] == 'line':
        return (sh['x1'], sh['y1']), (sh['x2'], sh['y2'])
    a, b = math.radians(sh['start_angle']), math.radians(sh['end_angle'])
    return ((sh['cx'] + sh['r'] * math.cos(a), sh['cy'] + sh['r'] * math.sin(a)),
            (sh['cx'] + sh['r'] * math.cos(b), sh['cy'] + sh['r'] * math.sin(b)))


def count_contours(shapes, tol=0.01):
    """Separate cut paths = pierces: every circle is one, the rest are joined by shared end points."""
    parent = {}

    def find(k):
        while parent.setdefault(k, k) != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    circles = 0
    for sh in shapes:
        if sh['type'] == 'circle':
            circles += 1
            continue
        a, b = (tuple(round(v / tol) for v in p) for p in _ends(sh))
        parent[find(a)] = find(b)
    return circles + len({find(k) for k in list(parent)})


def bbox(shapes):
    xs, ys = [], []
    for sh in shapes:
        if sh['type'] == 'line':
            xs += [sh['x1'], sh['x2']]
            ys += [sh['y1'], sh['y2']]
        else:
            xs += [sh['cx'] - sh['r'], sh['cx'] + sh['r']]
            ys += [sh['cy'] - sh['r'], sh['cy'] + sh['r']]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


# ---------- common XML bits ----------

def _technology(tech_el):
    """Technical: document material/thickness + one row per used channel (layer) with its cut parameters."""
    out = {'material': None, 'thickness': None, 'channels': []}
    if tech_el is None:
        return out
    out['material'] = tech_el.get('MaterialName')
    out['thickness'] = _f(tech_el.get('Thickness'), None)
    for ch in tech_el.iter('Channel'):
        port = ch.get('ChannelPort')
        cam = ch.find('CamParams')
        if port is None or cam is None or not port.isdigit() or int(port) > 16:     # 101.. = service channels
            continue
        cut = cam.find('Cut')
        pierce = cam.find('Pierce1')
        speed = _f(cam.get('WorkSpeed'), None)
        row = {'port': int(port), 'name': ch.get('MaterialName') or ch.get('Name'),
               'speed_mm_min': round(speed * 60, 1) if speed else None,
               'gas': None, 'gas_pressure_bar': None, 'power_w': None, 'focus_mm': None, 'height_mm': None,
               'pierce_time_sec': None}
        if cut is not None:
            row['gas'] = GAS.get(int(_f(cut.get('GasType'), 0)))
            row['gas_pressure_bar'] = _f(cut.get('GasPressure'), None)
            row['power_w'] = _f(cut.get('PeakPower'), None)
            row['focus_mm'] = _f(cut.get('Focus'), None)
            row['height_mm'] = _f(cut.get('Height'), None)
        if pierce is not None:
            ms = sum(_f(pierce.get(k)) for k in ('TimeFollow', 'TimeStay'))
            row['pierce_time_sec'] = round(ms / 1000, 3) if ms else None
        out['channels'].append(row)
    return out


def _plate_size(poly_el):
    pts = [_xy(p) for p in poly_el.iter('Point')] if poly_el is not None else []
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)


def _empty(fmt):
    return {'format': fmt, 'app_version': None, 'technology': {'material': None, 'thickness': None, 'channels': []},
            'parts': [], 'plates': [], 'results': [], 'skipped': {}}


def _finish_result(res, cut_shapes, plate_shapes, remnant_shapes):
    res['shapes'] = cut_shapes
    res['plate_shapes'] = plate_shapes
    res['remnant_shapes'] = remnant_shapes
    res['cut_length_mm'] = round(sum(shape_length(s) for s in cut_shapes), 1)
    res['pierces'] = count_contours(cut_shapes)
    if res.get('plate') is None and plate_shapes:
        b = bbox(plate_shapes)
        res['plate'] = (round(b[2] - b[0], 3), round(b[3] - b[1], 3))
    return res


# ---------- old format (.lxd) ----------

def _xml_entity_shapes(e, blocks, skipped, budget, depth=0):
    _spend(budget, 1)
    shapes = _xml_entity_raw(e, blocks, skipped, budget, depth)
    if e.tag != 'Insert':                     # an Insert's shapes were counted at its leaves
        _spend(budget, len(shapes))
    return shapes


def _xml_entity_raw(e, blocks, skipped, budget, depth):
    tag = e.tag
    if tag == 'Circle':
        c = _xy(e.find('Center'))
        return [{'type': 'circle', 'cx': c[0], 'cy': c[1], 'r': _f(e.get('Radius'))}]
    if tag == 'Arc':
        c = _xy(e.find('Center'))
        return [{'type': 'arc', 'cx': c[0], 'cy': c[1], 'r': _f(e.get('Radius')),
                 'start_angle': math.degrees(_f(e.get('StartAngle'))) % 360,
                 'end_angle': math.degrees(_f(e.get('EndAngle'))) % 360}]
    if tag == 'Line':
        p1 = _xy(e.find('StartPoint') if e.find('StartPoint') is not None else e.find('Point'))
        pts = e.findall('Point')
        p2 = _xy(e.find('EndPoint') if e.find('EndPoint') is not None else (pts[1] if len(pts) > 1 else None))
        return [{'type': 'line', 'x1': p1[0], 'y1': p1[1], 'x2': p2[0], 'y2': p2[1]}]
    if tag == 'LwPolyline':
        pts = [(_f(p.get('X')), _f(p.get('Y')), _f(p.get('Bulge'))) for p in e.findall('Point')]
        return _poly_shapes(pts, int(_f(e.get('PolyFlag'))) & 1)
    if tag == 'Insert' and depth < 4:
        block = blocks.get((e.findtext('BlockName') or '').strip())
        if block is None:
            return []
        shapes = []
        ents = block.find('Entities')
        for c in (ents if ents is not None else []):
            shapes += _xml_entity_shapes(c, blocks, skipped, budget, depth + 1)
        return _transform(shapes, _xy(block.find('BasePoint')), _xy(e.find('InsertPoint')),
                          _f(e.get('Angle') or e.findtext('Angle')),
                          _f(e.findtext('XScale'), 1.0), _f(e.findtext('YScale'), 1.0))
    skipped[tag] = skipped.get(tag, 0) + 1
    return []


def _parse_lxd(data):
    root = _fromstring(data)
    if root.tag != 'LXDDocument':
        raise ValueError('not an LXD document')
    out = _empty('lxd')
    out['technology'] = _technology(root.find('Technical'))
    blocks = {b.get('Name'): b for b in root.iter('Block')}
    for p in root.iter('NestPart'):
        out['parts'].append({'name': p.get('Name'), 'amount': int(_f(p.get('Amount'))),
                             'used': int(_f(p.get('AmountUsed'))), 'done': int(_f(p.get('AmountDone')))})
    plates = {}
    for p in root.iter('NestPlate'):
        size = _plate_size(p.find('LwPolyline'))
        plates[p.get('Handle')] = size
        out['plates'].append({'size': size, 'amount': int(_f(p.get('Amount'))), 'used': int(_f(p.get('AmountUsed')))})
    used_plates = [pl['size'] for pl in out['plates'] if pl['used']] or [pl['size'] for pl in out['plates']]
    budget = [0]
    for r in root.iter('NestResult'):
        res = {'name': r.get('Name'), 'utilization': _f(r.get('Utilization'), None),
               'plate_amount': int(_f(r.get('PlateAmount'), 1)) or 1, 'plate': used_plates[0] if used_plates else None,
               'counts': {}}
        cut, plate, remnant = [], [], []
        ents = r.find('Entities')
        for e in (ents if ents is not None else []):
            shapes = _xml_entity_shapes(e, blocks, out['skipped'], budget)
            if e.get('NestPlate'):
                plate += shapes
            elif e.get('LayerName') == 'Remnant':
                remnant += shapes
            else:
                cut += shapes
        if plate:                                    # the result's own sheet outline wins over the plate list
            res['plate'] = None
        out['results'].append(_finish_result(res, cut, plate, remnant))
    return out


# ---------- new format (.lxds) ----------

def _bin_shape(cls, rec, base, xml_el):
    """One Geometry2D record (class `cls`); `base` = offset of its payload."""
    if cls == 'LwPolyline':
        closed, count = struct.unpack_from('<II', rec, base + 8)
        if count > 100_000:
            raise ValueError('polyline too long')
        pts, o = [], base + 16
        for _ in range(count):
            x, y, b = struct.unpack_from('<3d', rec, o + 4)
            pts.append((x, y, b))
            o += 32                                  # u32 index, x, y, bulge, u32 0
        return _poly_shapes(pts, closed & 1 or int(_f(xml_el.get('PolyFlag'))) & 1)
    pts, scal, i = [], {}, base
    while i + 4 <= len(rec):                         # 4-byte aligned scan: point tag + 2 doubles
        if rec[i:i + 4] == POINT_TAG and i + 20 <= len(rec):
            pts.append(struct.unpack_from('<2d', rec, i + 4))
            scal.setdefault(len(pts), i + 20)
            i += 20
        else:
            i += 4
    if cls == 'Line' and len(pts) >= 2:
        return [{'type': 'line', 'x1': pts[0][0], 'y1': pts[0][1], 'x2': pts[1][0], 'y2': pts[1][1]}]
    if cls in ('Circle', 'Arc') and pts:
        o = scal[1]
        r = struct.unpack_from('<d', rec, o)[0]
        if cls == 'Circle':
            return [{'type': 'circle', 'cx': pts[0][0], 'cy': pts[0][1], 'r': r}]
        a, b = struct.unpack_from('<2d', rec, o + 12)
        return [{'type': 'arc', 'cx': pts[0][0], 'cy': pts[0][1], 'r': r,
                 'start_angle': math.degrees(a) % 360, 'end_angle': math.degrees(b) % 360}]
    if cls == 'PolyBezier':
        return _bezier_shapes(pts)
    return None


def _read_record(geo, addr):
    """-> (class name, record bytes, payload offset). Layout: d1d1d1d1 u32 u64 size 'S' u16 len name pad8,
    c1c1c1c1 u32 u64 size 'S' u16 len name pad8, payload, c0c0c0c0 d0d0d0d0."""
    if geo[addr:addr + 4] != b'\xd1\xd1\xd1\xd1':
        raise ValueError('bad geometry record')
    size = struct.unpack_from('<Q', geo, addr + 8)[0]
    rec = geo[addr:addr + 16 + size]
    o = 16
    if rec[o:o + 2] != b'S\x00':
        raise ValueError('bad geometry record')
    n = struct.unpack_from('<H', rec, o + 2)[0]
    o = (o + 4 + n + 7) // 8 * 8
    if rec[o:o + 4] != b'\xc1\xc1\xc1\xc1':
        raise ValueError('bad geometry record')
    n = struct.unpack_from('<H', rec, o + 18)[0]
    cls = rec[o + 20:o + 20 + n].decode('ascii', 'replace')
    return cls, rec, (o + 20 + n + 7) // 8 * 8


def _parse_lxds(zf):
    names = set(zf.namelist())

    def xml(name):
        return _fromstring(zf.read(name)) if name in names else None
    out = _empty('lxds')
    info = xml('info.xml')
    app = info.find('Application') if info is not None else None
    out['app_version'] = app.get('AppVer') if app is not None else None
    out['technology'] = _technology(xml('Technical/content.xml'))
    geo = zf.read('Geometry2D/data.bin') if 'Geometry2D/data.bin' in names else b''
    shapes_el = xml('Shapes2D/content.xml')
    shape_xml = {e.get('Handle'): e for e in (shapes_el if shapes_el is not None else [])}
    blocks_el = xml('Blocks/content.xml')
    blocks = {b.get('Name'): b for b in (blocks_el.iter('Block') if blocks_el is not None else [])}
    skipped = out['skipped']
    budget = [0]

    def handle_shapes(handle, depth=0):
        _spend(budget, 1)
        e = shape_xml.get(handle)
        if e is None:
            return []
        if e.tag == 'Insert' and depth < 4:
            return insert_shapes(e, depth)
        addr = e.get('GeoAddr')
        if addr is None:
            return []
        cls, rec, base = _read_record(geo, int(addr))
        got = _bin_shape(cls, rec, base, e)
        if got is None:
            skipped[cls] = skipped.get(cls, 0) + 1
            return []
        _spend(budget, len(got))
        return got

    def insert_shapes(e, depth=0):
        block = blocks.get((e.findtext('BlockName') or '').strip())
        if block is None:
            return []
        shapes = []
        for s in block.iter('Shape'):
            shapes += handle_shapes(s.get('Handle'), depth + 1)
        return _transform(shapes, _xy(block.find('BasePoint')), _xy(e.find('InsertPoint')),
                          _f(e.get('Angle') or e.findtext('Angle')),
                          _f(e.findtext('XScale'), 1.0), _f(e.findtext('YScale'), 1.0))

    parts_el = xml('Nest2D/Parts/content.xml')
    part_names = {}
    for p in (parts_el if parts_el is not None else []):
        part_names[p.get('Handle')] = p.get('Name')
        out['parts'].append({'name': p.get('Name'), 'amount': int(_f(p.get('Amount'))),
                             'used': int(_f(p.get('AmountUsed'))), 'done': int(_f(p.get('AmountDone')))})
    plates_el = xml('Nest2D/Plates/content.xml')
    plate_size = {}
    for p in (plates_el if plates_el is not None else []):
        size = _plate_size(p.find('LwPolyline'))
        plate_size[p.get('Handle')] = size
        out['plates'].append({'size': size, 'amount': int(_f(p.get('Amount'))), 'used': int(_f(p.get('AmountUsed')))})
    summary = xml('Nest2D/Results/content.xml')
    for s in (summary if summary is not None else []):
        r = xml(f"Nest2D/Results/{s.get('Index')}/content.xml")
        if r is None:
            continue
        res = {'name': s.get('Name'), 'utilization': _f(r.get('Utilization') or s.get('Utilization'), None),
               'plate_amount': int(_f(r.get('PlateAmount'), 1)) or 1, 'plate': None, 'counts': {}}
        cut, plate, remnant = [], [], []
        shapes_list = r.find('Shapes')
        for sh in (shapes_list if shapes_list is not None else []):
            if sh.get('Class') == 'Insert':
                name = part_names.get(sh.get('NestPart'), '?')
                res['counts'][name] = res['counts'].get(name, 0) + 1
                cut += insert_shapes(sh)
            elif sh.get('NestPlate'):
                res['plate'] = plate_size.get(sh.get('NestPlate'))
                plate += handle_shapes(sh.get('Handle'))
            else:
                e = shape_xml.get(sh.get('Handle'))
                (remnant if e is not None and e.get('LayerName') == 'Remnant' else cut).extend(
                    handle_shapes(sh.get('Handle')))
        out['results'].append(_finish_result(res, cut, plate, remnant))
    return out


def parse(path):
    """Parse an .lxd / .lxds file -> dict. Raises ValueError for anything that is not a readable nesting file."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                if sum(i.file_size for i in zf.infolist()) > 300 * 1024 * 1024:
                    raise ValueError('archive too large')
                return _parse_lxds(zf)
        with open(path, 'rb') as fh:
            return _parse_lxd(fh.read())
    except (ET.ParseError, struct.error, KeyError, zipfile.BadZipFile, UnicodeDecodeError, IndexError) as e:
        raise ValueError(f'unreadable nesting file: {e}') from e
