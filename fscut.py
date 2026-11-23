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
        elif sh['type'] == 'circle':
            xs += [sh['cx'] - sh['r'], sh['cx'] + sh['r']]
            ys += [sh['cy'] - sh['r'], sh['cy'] + sh['r']]
        else:                                    # arc: its end points + the axis points inside its CCW sweep
            for p in _ends(sh):
                xs.append(p[0])
                ys.append(p[1])
            sweep = (sh['end_angle'] - sh['start_angle']) % 360 or 360
            for k in range(4):
                if (k * 90 - sh['start_angle']) % 360 <= sweep:
                    xs.append(sh['cx'] + sh['r'] * round(math.cos(math.radians(k * 90))))
                    ys.append(sh['cy'] + sh['r'] * round(math.sin(math.radians(k * 90))))
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


# ---------- common XML bits ----------

# The technology fields a machine keeps per (Вид, thickness, gas, nozzle) - MachineCutTechnology's columns are built
# from these lists (app.py), so a new field goes here + in a new migration. (column, CamParams child (None = CamParams
# itself), attribute, label, unit, factor). A missing attribute is None (see _attr()).
CUT_FIELDS = [
    ('gas_pressure_bar', 'Cut', 'GasPressure', 'Налягане', 'bar', 1),
    ('peak_power_w', 'Cut', 'PeakPower', 'Мощност', 'W', 1),
    ('laser_current_pct', 'Cut', 'LaserCurrent', 'Ток', '%', 1),
    ('pwm_freq_hz', 'Cut', 'PwmFreq', 'Честота', 'Hz', 1),
    ('pwm_ratio_pct', 'Cut', 'PwmRatio', 'Запълване', '%', 100),
    ('focus_mm', 'Cut', 'Focus', 'Фокус', 'мм', 1),
    ('height_mm', 'Cut', 'Height', 'Височина', 'мм', 1),
    ('beam_size_mm', 'Cut', 'BeamSize', 'Лъч', 'мм', 1),
    ('lift_height_mm', None, 'LiftHeight', 'Повдигане', 'мм', 1),
]
PIERCE_STEPS = 3                 # steps with their own fields (owner, 2026-11-23); the rest stay in the raw parameters
STEP_FIELDS = [                  # column p<N>_<key> from element Pierce<N>; gas is a CUT_GASES key
    ('gas', 'GasType', 'Газ', '', None),
    ('pressure_bar', 'GasPressure', 'Налягане', 'bar', 1),
    ('power_w', 'PeakPower', 'Мощност', 'W', 1),
    ('current_pct', 'LaserCurrent', 'Ток', '%', 1),
    ('freq_hz', 'PwmFreq', 'Честота', 'Hz', 1),
    ('ratio_pct', 'PwmRatio', 'Запълване', '%', 100),
    ('focus_mm', 'Focus', 'Фокус', 'мм', 1),
    ('height_mm', 'Height', 'Височина', 'мм', 1),
    ('blow_ms', 'TimeBlow', 'Продухване', 'ms', 1),
    ('follow_ms', 'TimeFollow', 'Следене', 'ms', 1),
    ('stay_ms', 'TimeStay', 'Задържане', 'ms', 1),
]


def tech_columns():
    """[(column, label, unit, group)] of every technology field, in display order; a gas column ends in '_gas'."""
    cols = [(c, label, unit, 'Рязане') for c, _, _, label, unit, _ in CUT_FIELDS]
    cols.append(('pierce_steps', 'Стъпки', '', 'Пробиване'))
    for n in range(1, PIERCE_STEPS + 1):
        cols += [(f'p{n}_{k}', label, unit, f'Пробиване {n}') for k, _, label, unit, _ in STEP_FIELDS]
    return cols


def _gas(raw):
    """CypCut GasType -> CUT_GASES key; anything unknown (or not a number) -> None."""
    v = _f(raw, 0.0)
    return GAS.get(int(v)) if math.isfinite(v) and v == int(v) else None


def _attr(el, name, factor=1):
    """None when the element or the attribute is missing: CypCut drops zeros, but older versions also lack newer
    fields (PeakPower, BeamSize), so a missing value is shown as unknown rather than guessed as 0."""
    v = _f(el.get(name), None) if el is not None else None
    return None if v is None else round(v * factor, 6)


RAW_MAX_ROWS, RAW_MAX_DEPTH = 5000, 12      # a channel has ~40 rows, 3 levels


def _raw(el, path='', out=None, depth=0):
    """Every element under a channel as [path, attributes, text] - nothing of the technology is lost (a converter
    can write it back). ValueError past RAW_MAX_ROWS / RAW_MAX_DEPTH."""
    out = [] if out is None else out
    if depth > RAW_MAX_DEPTH:
        raise ValueError('technology nested too deep')
    total, seen = {}, {}
    for c in el:
        total[c.tag] = total.get(c.tag, 0) + 1
    for c in el:
        seen[c.tag] = seen.get(c.tag, 0) + 1
        p = f'{path}/{c.tag}' if path else c.tag
        if total[c.tag] > 1:
            p += f'[{seen[c.tag]}]'
        out.append([p, dict(c.attrib), (c.text or '').strip()])
        if len(out) > RAW_MAX_ROWS:
            raise ValueError('technology too large')
        _raw(c, p, out, depth + 1)
    return out


def _technology(tech_el):
    """Technical: document material/thickness + one row per used channel (layer): summary keys, `fields`
    (tech_columns() keys), `nozzle` and `raw` (_raw())."""
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
        speed = _f(cam.get('WorkSpeed'), None)
        fields = {c: _attr(cam if el is None else cam.find(el), attr, k) for c, el, attr, _, _, k in CUT_FIELDS}
        steps = _f(cam.get('PierceStepCount'), 1)
        steps = int(steps) if math.isfinite(steps) and 1 <= steps <= 9 else 1       # CypCut has Pierce1..9
        fields['pierce_steps'] = steps
        for n in range(1, PIERCE_STEPS + 1):
            p = cam.find(f'Pierce{n}')
            for key, attr, _, _, k in STEP_FIELDS:
                fields[f'p{n}_{key}'] = (_gas(p.get(attr)) if p is not None else None) \
                    if key == 'gas' else _attr(p, attr, k)
        pierce_ms = sum(_f(p.get('TimeFollow')) + _f(p.get('TimeStay'))          # every used step
                        for p in (cam.find(f'Pierce{n}') for n in range(1, steps + 1)) if p is not None)
        cut = cam.find('Cut')
        mp = ch.find('.//MaterialParams')
        out['channels'].append({
            'port': int(port), 'name': ch.get('MaterialName') or ch.get('Name'),
            'speed_mm_min': round(speed * 60, 1) if speed else None,
            'gas': _gas(cut.get('GasType')) if cut is not None else None,
            'gas_pressure_bar': fields['gas_pressure_bar'], 'power_w': fields['peak_power_w'],
            'focus_mm': fields['focus_mm'], 'height_mm': fields['height_mm'],
            'pierce_time_sec': round(pierce_ms / 1000, 3) if pierce_ms else None,
            'nozzle': ((mp.get('NozzleSpec') or '').strip() or None) if mp is not None else None,
            'fields': fields, 'raw': _raw(ch)})
    return out


def _plate_size(poly_el):
    pts = [_xy(p) for p in poly_el.iter('Point')] if poly_el is not None else []
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)


def _empty(fmt):
    return {'format': fmt, 'app_version': None, 'technology': {'material': None, 'thickness': None, 'channels': []},
            'parts': [], 'plates': [], 'results': [], 'model': None, 'skipped': {}}


def _model(name, shapes):
    """A part file's own drawing (no nest): its shapes, size, cut length and pierces - or None when empty."""
    if not shapes:
        return None
    b = bbox(shapes)
    return {'name': name, 'shapes': shapes, 'size': (round(b[2] - b[0], 3), round(b[3] - b[1], 3)),
            'cut_length_mm': round(sum(shape_length(s) for s in shapes), 1), 'pierces': count_contours(shapes)}


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
    # a part file: the drawing sits in the document's own Entities (no nest result)
    own = root.find('Entities')
    shapes = []
    for e in (own if own is not None else []):
        shapes += _xml_entity_shapes(e, blocks, out['skipped'], budget)
    out['model'] = _model(None, shapes)
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
    # a part file: Model2D lists the part's own shapes (each with its GeoAddr)
    model = xml('Model2D/content.xml')
    if model is not None:
        shapes = []
        for sh in model.iter('Shape'):
            shape_xml.setdefault(sh.get('Handle'), sh)
            shapes += handle_shapes(sh.get('Handle'))
        out['model'] = _model(model.get('Name'), shapes)
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
    except (ET.ParseError, struct.error, KeyError, zipfile.BadZipFile, UnicodeDecodeError, IndexError,
            OverflowError, RecursionError) as e:
        raise ValueError(f'unreadable nesting file: {e}') from e


# ---------- writing the old format (.lxd) ----------
# Step 1 of the converters (owner's decision 2026-11-23): one part (a detail's drawing) + its technology as an old
# CypCutE .lxd. Only entity syntax seen in real old files is written: Circle and LwPolyline (lines/arcs are chained
# into polylines with DXF bulges - CypCut cuts closed contours). The .lxds writer comes after the owner tried these.

GAS_CODE = {'air': 1, 'oxygen': 2, 'nitrogen': 3}
LXD_EMPTY_PORTS = 15            # an old file lists Channel ports 1..15


def _num(v):
    """CypCut style number text: shortest form, '.5' for 0.5, '-.5' for -0.5, never '-0'."""
    s = f'{round(float(v), 9):.9g}'
    if 'e' in s:
        s = f'{float(v):.9f}'.rstrip('0').rstrip('.')
    if s in ('-0', ''):
        return '0'
    if s.startswith('0.'):
        return s[1:]
    if s.startswith('-0.'):
        return '-' + s[2:]
    return s


def chain(shapes, tol=0.01):
    """Lines/arcs joined end to start into polylines -> [('circle', cx, cy, r) | ('poly', [(x, y, bulge)], closed)].
    Open chains are walked from a free end; a piece may be walked backwards (its bulge changes sign)."""
    def key(p):
        return round(p[0] / tol), round(p[1] / tol)
    segs, out = [], []
    for sh in shapes:
        if sh['type'] == 'circle':
            out.append(('circle', sh['cx'], sh['cy'], sh['r']))
            continue
        a, b = _ends(sh)
        if sh['type'] == 'line' and math.dist(a, b) < tol:
            continue
        sweep = ((sh['end_angle'] - sh['start_angle']) % 360 or 360) if sh['type'] == 'arc' else 0
        segs.append((a, b, math.tan(math.radians(sweep) / 4)))          # bulge of the CCW arc start -> end
    by_end = {}
    for i, (a, b, _) in enumerate(segs):
        by_end.setdefault(key(a), []).append(i)
        by_end.setdefault(key(b), []).append(i)

    def free(i):
        return len(by_end[key(segs[i][0])]) == 1 or len(by_end[key(segs[i][1])]) == 1
    used = [False] * len(segs)
    for start in [i for i in range(len(segs)) if free(i)] + list(range(len(segs))):
        if used[start]:
            continue
        a, b, bulge = segs[start]
        if len(by_end[key(b)]) == 1 and len(by_end[key(a)]) != 1:       # walk away from the free end
            a, b, bulge = b, a, -bulge
        used[start] = True
        pts, cur = [(a[0], a[1], bulge)], b
        while True:
            nxt = next((j for j in by_end.get(key(cur), []) if not used[j]), None)
            if nxt is None:
                break
            used[nxt] = True
            na, nb, nbulge = segs[nxt]
            if key(na) != key(cur):
                na, nb, nbulge = nb, na, -nbulge
            pts.append((na[0], na[1], nbulge))
            cur = nb
        closed = key(cur) == key(pts[0][:2])
        if not closed:
            pts.append((cur[0], cur[1], 0.0))
        out.append(('poly', pts, closed))
    return out


def _channel_el(port, raw=None, fields=None, speed_mm_min=None, gas=None):
    """<Channel> with its CamParams: the stored parameter tree (fscut._raw() rows) rebuilt as it was, then the speed,
    gas and every known field written on top (values edited in the dossier win). A field with no value is left as
    the tree has it."""
    ch = ET.Element('Channel', {'ChannelPort': str(port), 'ParamFlag': '768', 'OEM': '1'})
    made = {}
    for path, attrs, text in raw or []:
        parent_path, _, last = path.rpartition('/')
        el = ET.SubElement(made.get(parent_path, ch), last.split('[')[0], {k: str(v) for k, v in attrs.items()})
        if text:
            el.text = text
        made[path] = el
    cam = made.get('CamParams')
    if cam is None:
        cam = ET.SubElement(ch, 'CamParams')

    def child(tag):
        el = cam.find(tag)
        return el if el is not None else ET.SubElement(cam, tag)
    fields = fields or {}
    if speed_mm_min:
        cam.set('WorkSpeed', _num(speed_mm_min / 60))
    if gas in GAS_CODE:
        child('Cut').set('GasType', str(GAS_CODE[gas]))
    for col, el, attr, _, _, k in CUT_FIELDS:
        if fields.get(col) is not None:
            (cam if el is None else child(el)).set(attr, _num(fields[col] / k))
    if fields.get('pierce_steps'):
        cam.set('PierceStepCount', str(int(fields['pierce_steps'])))
    for n in range(1, PIERCE_STEPS + 1):
        for key, attr, _, _, k in STEP_FIELDS:
            v = fields.get(f'p{n}_{key}')
            if v is None or (key == 'gas' and v not in GAS_CODE):
                continue
            child(f'Pierce{n}').set(attr, str(GAS_CODE[v]) if key == 'gas' else _num(v / k))
    return ch


def write_lxd(shapes, channel=None):
    """One part as an old-format .lxd (bytes, CRLF like CypCut). `channel` = {'raw', 'fields', 'speed_mm_min', 'gas'}
    (any may be missing) or None = an empty technology (CypCut uses its own). ValueError without geometry."""
    import uuid
    items = chain(shapes)
    if not items:
        raise ValueError('no geometry')
    handle = [1000]

    def h():
        handle[0] += 1
        return str(handle[0])
    b = bbox(shapes)
    doc = ET.Element('LXDDocument')
    head = ET.SubElement(doc, 'DocHeader', {'DefaultChannel': '1', 'HandleSeed': '0',
                                             'ID': '{' + str(uuid.uuid4()).upper() + '}'})
    ET.SubElement(head, 'ExtMin', {'X': _num(b[0]), 'Y': _num(b[1])})
    ET.SubElement(head, 'ExtMax', {'X': _num(b[2]), 'Y': _num(b[3])})
    ET.SubElement(head, 'InsertBase')
    ET.SubElement(head, 'HomeRef', {'Enabled': 'true', 'RefType': '3', 'X': _num(b[0]), 'Y': _num(b[1])})
    chans = ET.SubElement(ET.SubElement(doc, 'Technical'), 'Channels')
    ET.SubElement(chans, 'Channel', {'ParamFlag': '256'})
    c = channel or {}
    chans.append(_channel_el(1, c.get('raw'), c.get('fields'), c.get('speed_mm_min'), c.get('gas')))
    for port in range(2, LXD_EMPTY_PORTS + 1):
        ET.SubElement(chans, 'Channel', {'ChannelPort': str(port)})
    vp = ET.SubElement(ET.SubElement(doc, 'Viewports', {'Handle': '3'}), 'VPort', {'Handle': h()})
    ET.SubElement(vp, 'ViewCenter')
    ET.SubElement(vp, 'ViewHeight').text = '100'
    ET.SubElement(vp, 'AspectRatio').text = '1'
    rect = ET.SubElement(vp, 'DisplayRect')
    ET.SubElement(rect, 'Right').text = '1024'
    ET.SubElement(rect, 'Bottom').text = '768'
    for tag, hd in (('TextStyles', '6'), ('Layers', '5'), ('Blocks', '4')):
        ET.SubElement(doc, tag, {'Handle': hd})
    ents = ET.SubElement(doc, 'Entities', {'Handle': '2'})
    for it in items:
        common = {'Handle': h(), 'ChannelPort': '1', 'LayerName': '0'}
        if it[0] == 'circle':
            el = ET.SubElement(ents, 'Circle', {**common, 'Radius': _num(it[3])})
            ET.SubElement(el, 'ToolCompensation')
            ET.SubElement(el, 'MicroConnects')
            ET.SubElement(el, 'Center', {'X': _num(it[1]), 'Y': _num(it[2])})
            continue
        pts, closed = it[1], it[2]
        el = ET.SubElement(ents, 'LwPolyline', {**common, 'PointCount': str(len(pts)), 'PolyFlag': '1' if closed else '0'})
        ET.SubElement(el, 'ToolCompensation')
        ET.SubElement(el, 'MicroConnects')
        for x, y, bulge in pts:
            a = {'X': _num(x), 'Y': _num(y)}
            if abs(bulge) > 1e-12:
                a['Bulge'] = _num(bulge)
            ET.SubElement(el, 'Point', a)
    for tag, hd in (('NestParts', '7'), ('NestPlates', '8'), ('NestGroups', '9'), ('NestResults', '10')):
        ET.SubElement(doc, tag, {'Handle': hd})
    head.set('HandleSeed', str(handle[0] + 1))
    ET.indent(doc, '\t')
    text = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(doc, encoding='unicode') + '\n'
    return text.replace('\n', '\r\n').encode('utf-8')
