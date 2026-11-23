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


# ---------- cutting attributes of a part's contours (lead-in, micro-joints, compensation) ----------
# Kept for part files so a converted .lxd keeps what CypCut set (owner's decision 2026-11-23: only what the source
# file has - nothing is generated). An "entity" = one source Circle / LwPolyline with its geometry exactly as stored
# (Pos values count along its own vertices) + 'attrs' (StartAngle, PathStartParam) + 'cam' [(tag, attrs, children)].

CAM_TAGS = ('LeadIn', 'ToolCompensation', 'MicroConnects')


def _cam_xml(e):
    """LeadIn / ToolCompensation / MicroConnects children of an XML shape (2 levels; a MicroConnect's own LeadIn of the
    new format is left out - not seen in old files)."""
    return [(c.tag, dict(c.attrib), [(g.tag, dict(g.attrib)) for g in c]) for c in e if c.tag in CAM_TAGS]


def _joint_leads(e):
    """Lead-in length of each micro-joint of a shape (new format: a MicroConnect may carry its own LeadIn = the head
    pierces again after the gap); 0 = none (cutting just resumes)."""
    return [_f(mc.find('LeadIn').get('Length')) if mc.find('LeadIn') is not None else 0.0
            for mc in e.iterfind('MicroConnects/MicroConnect')]


def _xml_entity(e):
    """Old-format Circle / LwPolyline -> entity, or None for anything else."""
    attrs = {k: e.get(k) for k in ('StartAngle', 'IsFill', 'PathStartParam') if e.get(k) is not None}
    port = int(e.get('ChannelPort')) if (e.get('ChannelPort') or '').isdigit() else 1     # the layer it is cut with
    if e.tag == 'Circle':
        c = _xy(e.find('Center'))
        return {'kind': 'circle', 'cx': c[0], 'cy': c[1], 'r': _f(e.get('Radius')), 'attrs': attrs, 'cam': _cam_xml(e),
                'port': port, 'joint_leads': _joint_leads(e)}
    if e.tag == 'LwPolyline':
        pts = [(_f(p.get('X')), _f(p.get('Y')), _f(p.get('Bulge'))) for p in e.findall('Point')]
        return {'kind': 'poly', 'pts': pts, 'closed': bool(int(_f(e.get('PolyFlag'))) & 1), 'attrs': attrs,
                'cam': _cam_xml(e), 'port': port, 'joint_leads': _joint_leads(e)}
    return None


def _entity_shapes(ent):
    if ent['kind'] == 'circle':
        return [{'type': 'circle', 'cx': ent['cx'], 'cy': ent['cy'], 'r': ent['r']}]
    return _poly_shapes(ent['pts'], ent['closed'])


def _sub_payload(rec, name):
    """Offset of the payload of the first sub-record called `name` inside a Shapes2D/data.bin record, or None.
    Sub-record: c1c1c1c1 u32 u64 size 'S' 00 u16 len name pad8 payload c0c0c0c0 (pad relative to the record start)."""
    key = b'S\x00' + struct.pack('<H', len(name)) + name.encode('ascii')
    i = rec.find(key)
    if i < 16:
        return None
    return (i + 4 + len(name) + 7) // 8 * 8


def _bin_cam(data, xml_el, kind):
    """Lead-in (LeadParam) and compensation (Curve) of a new-format shape from Shapes2D/data.bin, + its XML
    micro-joints. Layout checked against the same part saved by CypCut as .lxd and .lxds (docs/FSCUT_FORMAT.md)."""
    cam, attrs, port = [], {}, 1
    addr = xml_el.get('DataAddr')
    rec = b''
    if addr is not None and data[int(addr):int(addr) + 4] == b'\xd1\xd1\xd1\xd1':
        a = int(addr)
        rec = data[a:a + 16 + struct.unpack_from('<Q', data, a + 8)[0]]
    o = _sub_payload(rec, 'Shape')                          # its first u32 = the layer (ChannelPort)
    if o is not None and o + 4 <= len(rec):
        port = struct.unpack_from('<I', rec, o)[0] or 1
    o = _sub_payload(rec, 'LeadParam')
    if o is not None:
        typ = struct.unpack_from('<I', rec, o)[0]
        pos, ang, length, arc_r, hole_r = struct.unpack_from('<5d', rec, o + 16)
        lead = {'LeadType': str(typ)}
        if kind == 'poly' and pos:
            lead['Pos'] = _num(pos)
            attrs['PathStartParam'] = _num(pos)
        lead.update({'Angle': _num(ang), 'Length': _num(length), 'ArcRadius': _num(arc_r), 'LeadHoleR': _num(hole_r)})
        cam.append(('LeadIn', lead, []))
    o = _sub_payload(rec, 'Curve')
    comp = {}
    if o is not None:
        if struct.unpack_from('<I', rec, o)[0] & 1:            # u32 filled, u32 0, u32 1, u32 style, double size
            attrs['IsFill'] = 'true'
        style = struct.unpack_from('<I', rec, o + 12)[0]
        if style:
            comp = {'Style': str(style), 'Size': _num(struct.unpack_from('<d', rec, o + 16)[0])}
    cam.append(('ToolCompensation', comp, []))
    cam += [c for c in _cam_xml(xml_el) if c[0] == 'MicroConnects']
    return cam, attrs, port


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


def library_name_parts(name):
    """A CypCut library file name -> (thickness mm, nozzle): "8mm1.2d1.5d_D.fsm" -> (8.0, "1.2d/1.5d"),
    "16mm1.4E.fsm" -> (16.0, "1.4E"), "1mm1.5s2.0s_0.6_n2_D.fsm" -> (1.0, "1.5s/2.0s"). (None, None) when the name
    does not start with "<number>mm"; text after the first "_" is the user's own suffix."""
    import re
    m = re.match(r'\s*(\d+(?:[.,]\d+)?)\s*mm([^_]*)', name or '', re.I)
    if not m:
        return None, None
    thickness = float(m.group(1).replace(',', '.'))
    rest = re.sub(r'\.fsm$', '', m.group(2), flags=re.I)
    nozzles = re.findall(r'\d+(?:\.\d+)?[A-Za-z]+', rest)
    return (thickness if 0 < thickness < 1000 else None), ('/'.join(nozzles)[:30] or None)


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
        library = (ch.findtext('MaterialName') or '').strip() or None
        lib_thickness, lib_nozzle = library_name_parts(library)
        out['channels'].append({
            'port': int(port), 'name': ch.get('MaterialName') or ch.get('Name'),
            # the CypCut library technology the layer was loaded from ("8mm1.2d1.5d_D.fsm"): its thickness and
            # nozzle, which MaterialParams does not keep (CypCut overwrites it with the document's)
            'library': library, 'library_thickness': lib_thickness, 'library_nozzle': lib_nozzle,
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


def _model(name, shapes, entities=(), loose=None):
    """A part file's own drawing (no nest): its shapes, size, cut length and pierces - or None when empty.
    `entities` = its Circle/LwPolyline contours with their cutting attributes, `loose` = the other shapes."""
    if not shapes:
        return None
    b = bbox(shapes)
    loose = shapes if loose is None else loose
    return {'name': name, 'shapes': shapes, 'entities': list(entities),
            'pieces': [(e, _entity_shapes(e)) for e in entities] + ([(None, loose)] if loose else []),
            'loose': loose, 'size': (round(b[2] - b[0], 3), round(b[3] - b[1], 3)),
            'cut_length_mm': round(sum(shape_length(s) for s in shapes), 1), 'pierces': count_contours(shapes)}


def cut_stats(pieces, channels=()):
    """Cut length, pierces and time of [(entity|None, shapes)] as the file has them cut (owner, 2026-11-24):
    a contour + its lead-in (`Length`) - its micro-joint gaps (`Size` each); compensation (`Style` != 0) moves a closed
    contour by `Size` - outward for the outer one (IsFill), inward for a hole: +-2*pi*Size. One pierce per contour, plus
    one per micro-joint with its own lead-in (new format; its lead length is cut too). Shapes with no cutting attributes
    (lines/arcs, a moved block) count as drawn. Time per layer (the entity's ChannelPort) = length / that layer's speed
    + pierces x its pierce time; a layer the technology does not list takes the first channel's values.
    ponytail: no rapid moves, slow lead/end, corner slow-downs or lead-outs - an estimate, CypCut's own is the real one.
    -> {'cut_mm', 'lead_mm', 'gap_mm', 'pierces', 'micro_joints', 'minutes' (None when a used layer has no speed),
    'layers': [{'port', 'cut_mm', 'lead_mm', 'pierces', 'speed_mm_min', 'pierce_time_sec', 'minutes'}]}"""
    chans = {c['port']: c for c in channels}
    first = channels[0] if channels else {}
    layers, gap, joints = {}, 0.0, 0
    for ent, shapes in pieces:
        length = sum(shape_length(sh) for sh in shapes)
        if ent is None:
            port, lead, n = 1, 0.0, count_contours(shapes) if shapes else 0
        else:
            cam = {tag: (attrs, kids) for tag, attrs, kids in ent.get('cam') or []}
            port, n = ent.get('port') or 1, 1
            lead = _f(cam.get('LeadIn', ({}, []))[0].get('Length'))
            comp = cam.get('ToolCompensation', ({}, []))[0]
            if _f(comp.get('Style')) and (ent['kind'] == 'circle' or ent.get('closed')):
                sign = 1 if ent['attrs'].get('IsFill') == 'true' else -1
                length = max(length + sign * 2 * math.pi * _f(comp.get('Size')), 0.0)
            kids = cam.get('MicroConnects', ({}, []))[1]
            g = sum(_f(a.get('Size')) for _, a in kids)
            gap += g
            joints += len(kids)
            length = max(length - g, 0.0)
            joint_leads = [x for x in ent.get('joint_leads') or [] if x > 0]
            n += len(joint_leads)
            lead += sum(joint_leads)
        row = layers.setdefault(port, {'port': port, 'cut_mm': 0.0, 'lead_mm': 0.0, 'pierces': 0})
        row['cut_mm'] += length
        row['lead_mm'] += lead
        row['pierces'] += n
    total_min = 0.0
    for row in layers.values():
        ch = chans.get(row['port']) or first
        row['speed_mm_min'], row['pierce_time_sec'] = ch.get('speed_mm_min'), ch.get('pierce_time_sec')
        row['minutes'] = ((row['cut_mm'] + row['lead_mm']) / row['speed_mm_min']
                          + row['pierces'] * (row['pierce_time_sec'] or 0) / 60) if row['speed_mm_min'] else None
        total_min = None if total_min is None or row['minutes'] is None else total_min + row['minutes']
        row['cut_mm'], row['lead_mm'] = round(row['cut_mm'], 1), round(row['lead_mm'], 1)
    rows = sorted(layers.values(), key=lambda r: r['port'])
    return {'cut_mm': round(sum(r['cut_mm'] for r in rows), 1), 'lead_mm': round(sum(r['lead_mm'] for r in rows), 1),
            'gap_mm': round(gap, 1), 'pierces': sum(r['pierces'] for r in rows), 'micro_joints': joints,
            'minutes': round(total_min, 2) if total_min is not None and rows else None, 'layers': rows}


def _sig(shapes):
    """Rotation-free fingerprint of a part: contours + cut length (0.5 mm)."""
    return count_contours(shapes), round(sum(shape_length(s) for s in shapes) * 2)


def _group_placed(pieces, parts):
    """An old-format nest result stores the placed parts flattened into plain contours. pieces = [(entity|None,
    shapes)] -> (placed [{'part': index|None, 'pieces'}], extra pieces). One placed part = an outer contour (IsFill, or
    by containment when none is marked) + every piece whose box lies in its box (the smallest one); it is matched to a
    NestPart by _sig(). A piece in no outer contour (a cut-off line) is extra."""
    boxes = [bbox(sh) if sh else None for _, sh in pieces]
    closed = [i for i, (e, sh) in enumerate(pieces) if e is not None and (e['kind'] == 'circle' or e['closed'])]
    outer = [i for i in closed if pieces[i][0]['attrs'].get('IsFill') == 'true']

    def inside(b, o):
        return o[0] - 1e-6 <= b[0] and o[1] - 1e-6 <= b[1] and b[2] <= o[2] + 1e-6 and b[3] <= o[3] + 1e-6
    if not outer:
        outer = [i for i in closed if not any(j != i and inside(boxes[i], boxes[j]) and boxes[i] != boxes[j]
                                              for j in closed)]
    groups, extra = {i: [i] for i in outer}, []
    for i, b in enumerate(boxes):
        if i in groups or b is None:
            continue
        hosts = [o for o in outer if inside(b, boxes[o])]
        if hosts:
            groups[min(hosts, key=lambda o: (boxes[o][2] - boxes[o][0]) * (boxes[o][3] - boxes[o][1]))].append(i)
        else:
            extra.append(pieces[i])
    sigs = {}
    for n, p in enumerate(parts):
        sigs.setdefault(_sig([s for _, sh in p.get('pieces', []) for s in sh]), n)
    placed = []
    for o in outer:
        members = [pieces[i] for i in sorted(groups[o])]
        placed.append({'part': sigs.get(_sig([s for _, sh in members for s in sh])), 'pieces': members})
    return placed, extra


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
    budget = [0]

    def pieces_of(container):
        """Entities of a Block / NestResult -> [(entity|None, shapes)] (entity for Circle / LwPolyline)."""
        out_p = []
        for e in (container if container is not None else []):
            got = _xml_entity_shapes(e, blocks, out['skipped'], budget)
            if got:
                out_p.append((_xml_entity(e), got))
        return out_p
    for p in root.iter('NestPart'):
        block = blocks.get((p.findtext('Insert/BlockName') or '').strip())
        out['parts'].append({'name': p.get('Name'), 'amount': int(_f(p.get('Amount'))),
                             'used': int(_f(p.get('AmountUsed'))), 'done': int(_f(p.get('AmountDone'))),
                             'pieces': pieces_of(block.find('Entities')) if block is not None else []})
    plates = {}
    for p in root.iter('NestPlate'):
        size = _plate_size(p.find('LwPolyline'))
        plates[p.get('Handle')] = size
        out['plates'].append({'size': size, 'amount': int(_f(p.get('Amount'))), 'used': int(_f(p.get('AmountUsed'))),
                              'name': p.get('Name')})
    used_plates = [pl['size'] for pl in out['plates'] if pl['used']] or [pl['size'] for pl in out['plates']]
    for r in root.iter('NestResult'):
        res = {'name': r.get('Name'), 'utilization': _f(r.get('Utilization'), None),
               'plate_amount': int(_f(r.get('PlateAmount'), 1)) or 1, 'plate': used_plates[0] if used_plates else None,
               'counts': {}, 'plate_outline': None}
        cut, plate, remnant, pieces = [], [], [], []
        ents = r.find('Entities')
        for e in (ents if ents is not None else []):
            shapes = _xml_entity_shapes(e, blocks, out['skipped'], budget)
            if e.get('NestPlate'):
                plate += shapes
                res['plate_outline'] = _xml_entity(e)
            elif e.get('LayerName') == 'Remnant':
                remnant += shapes
            else:
                cut += shapes
                if shapes:
                    pieces.append((_xml_entity(e), shapes))
        if plate:                                    # the result's own sheet outline wins over the plate list
            res['plate'] = None
        res['placed'], res['extra'] = _group_placed(pieces, out['parts'])
        for pl in res['placed']:                    # the old format cannot say it - counted from the matched parts
            name = out['parts'][pl['part']]['name'] if pl['part'] is not None else '?'
            res['counts'][name] = res['counts'].get(name, 0) + 1
        out['results'].append(_finish_result(res, cut, plate, remnant))
    # a part file: the drawing sits in the document's own Entities (no nest result)
    own = root.find('Entities')
    shapes, entities, loose = [], [], []
    for e in (own if own is not None else []):
        got = _xml_entity_shapes(e, blocks, out['skipped'], budget)
        shapes += got
        ent = _xml_entity(e)
        if ent is not None:
            entities.append(ent)
        else:
            loose += got
    out['model'] = _model(None, shapes, entities, loose)
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


def _bin_entity(geo, data, xml_el):
    """New-format Circle / LwPolyline of a part -> entity (see _xml_entity), or None for anything else."""
    cls = xml_el.get('Class') if xml_el.tag == 'Shape' else xml_el.tag
    if cls not in ('Circle', 'LwPolyline') or xml_el.get('GeoAddr') is None:
        return None
    cls, rec, base = _read_record(geo, int(xml_el.get('GeoAddr')))
    kind = 'circle' if cls == 'Circle' else 'poly'
    cam, attrs, port = _bin_cam(data, xml_el, kind)
    if kind == 'circle':
        if rec[base:base + 4] != POINT_TAG:
            return None
        cx, cy, r = struct.unpack_from('<3d', rec, base + 4)
        start = struct.unpack_from('<d', rec, base + 32)[0]       # point, r, u32 0, start angle (rad)
        if start:
            attrs['StartAngle'] = _num(start)
        return {'kind': 'circle', 'cx': cx, 'cy': cy, 'r': r, 'attrs': attrs, 'cam': cam, 'port': port,
                'joint_leads': _joint_leads(xml_el)}
    path_start, closed, count = struct.unpack_from('<dII', rec, base)
    if path_start:
        attrs['PathStartParam'] = _num(path_start)
    if count > 100_000:
        raise ValueError('polyline too long')
    pts = [struct.unpack_from('<3d', rec, base + 16 + 32 * i + 4) for i in range(count)]
    return {'kind': 'poly', 'pts': pts, 'closed': bool(closed & 1 or int(_f(xml_el.get('PolyFlag'))) & 1),
            'attrs': attrs, 'cam': cam, 'port': port,
                'joint_leads': _joint_leads(xml_el)}


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

    attr_data = zf.read('Shapes2D/data.bin') if 'Shapes2D/data.bin' in names else b''

    def piece(handle, depth=0):
        """(entity|None, shapes) of one shape handle (entity for a Circle / LwPolyline)."""
        got = handle_shapes(handle, depth)
        e = shape_xml.get(handle)
        return (_bin_entity(geo, attr_data, e) if got and e is not None else None), got

    def insert_pieces(e, depth=0):
        """The pieces of an Insert's block. A result's {GUID} block holds the part already placed; an Insert that
        still moves / turns its block gives the moved shapes without cutting attributes."""
        block = blocks.get((e.findtext('BlockName') or '').strip())
        if block is None or depth >= 4:
            return []
        out_p = [piece(s.get('Handle'), depth + 1) for s in block.iter('Shape')]
        args = (_xy(block.find('BasePoint')), _xy(e.find('InsertPoint')), _f(e.get('Angle') or e.findtext('Angle')),
                _f(e.findtext('XScale'), 1.0), _f(e.findtext('YScale'), 1.0))
        if args != ((0.0, 0.0), (0.0, 0.0), 0.0, 1.0, 1.0):
            out_p = [(None, _transform(sh, *args)) for _, sh in out_p]
        return [p for p in out_p if p[1]]

    parts_el = xml('Nest2D/Parts/content.xml')
    part_names, part_index = {}, {}
    for p in (parts_el if parts_el is not None else []):
        part_names[p.get('Handle')] = p.get('Name')
        part_index[p.get('Handle')] = len(out['parts'])
        pieces = []
        for sh in p.findall('Shape'):
            if sh.get('Class') == 'Insert':
                pieces += insert_pieces(sh)
            else:
                got = piece(sh.get('Handle'))
                if got[1]:
                    pieces.append(got)
        out['parts'].append({'name': p.get('Name'), 'amount': int(_f(p.get('Amount'))),
                             'used': int(_f(p.get('AmountUsed'))), 'done': int(_f(p.get('AmountDone'))),
                             'pieces': pieces})
    plates_el = xml('Nest2D/Plates/content.xml')
    plate_size = {}
    for p in (plates_el if plates_el is not None else []):
        size = _plate_size(p.find('LwPolyline'))
        plate_size[p.get('Handle')] = size
        out['plates'].append({'size': size, 'amount': int(_f(p.get('Amount'))), 'used': int(_f(p.get('AmountUsed'))),
                              'name': p.get('Name')})
    summary = xml('Nest2D/Results/content.xml')
    for s in (summary if summary is not None else []):
        r = xml(f"Nest2D/Results/{s.get('Index')}/content.xml")
        if r is None:
            continue
        res = {'name': s.get('Name'), 'utilization': _f(r.get('Utilization') or s.get('Utilization'), None),
               'plate_amount': int(_f(r.get('PlateAmount'), 1)) or 1, 'plate': None, 'counts': {},
               'plate_outline': None, 'placed': [], 'extra': []}
        cut, plate, remnant = [], [], []
        shapes_list = r.find('Shapes')
        for sh in (shapes_list if shapes_list is not None else []):
            if sh.get('Class') == 'Insert':
                name = part_names.get(sh.get('NestPart'), '?')
                res['counts'][name] = res['counts'].get(name, 0) + 1
                pieces = insert_pieces(sh)
                res['placed'].append({'part': part_index.get(sh.get('NestPart')), 'pieces': pieces})
                cut += [s for _, got in pieces for s in got]
            elif sh.get('NestPlate'):
                res['plate'] = plate_size.get(sh.get('NestPlate'))
                ent, got = piece(sh.get('Handle'))
                plate += got
                res['plate_outline'] = ent
            else:
                e = shape_xml.get(sh.get('Handle'))
                ent, got = piece(sh.get('Handle'))
                if e is not None and e.get('LayerName') == 'Remnant':
                    remnant += got
                else:
                    cut += got
                    if got:
                        res['extra'].append((ent, got))
        out['results'].append(_finish_result(res, cut, plate, remnant))
    # a part file: Model2D lists the part's own shapes (each with its GeoAddr)
    model = xml('Model2D/content.xml')
    if model is not None:
        shapes, entities, loose = [], [], []
        for sh in model.iter('Shape'):
            shape_xml.setdefault(sh.get('Handle'), sh)
            got = handle_shapes(sh.get('Handle'))
            shapes += got
            ent = _bin_entity(geo, attr_data, shape_xml[sh.get('Handle')]) if got else None
            if ent is not None:
                entities.append(ent)
            else:
                loose += got
        out['model'] = _model(model.get('Name'), shapes, entities, loose)
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


def gas_codes(gas_map):
    """{gas: channel gas} (GAS_CODE keys, e.g. {'air': 'nitrogen'} = air blown through the N2 channel) -> GasType text
    replacements, high pressure included (code + 3: 4/5/6 = air/O2/N2 high). Empty for none / identity."""
    out = {}
    for g, ch in (gas_map or {}).items():
        if g in GAS_CODE and ch in GAS_CODE and g != ch:
            out[str(GAS_CODE[g])] = str(GAS_CODE[ch])
            out[str(GAS_CODE[g] + 3)] = str(GAS_CODE[ch] + 3)
    return out
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


def _channel_el(port, raw=None, fields=None, speed_mm_min=None, gas=None, gas_map=None):
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
    codes = gas_codes(gas_map)          # the machine's gas -> channel wiring (Machine.gas_channels)
    if codes:
        for el in cam.iter():
            if el.get('GasType') in codes:
                el.set('GasType', codes[el.get('GasType')])
    return ch


def _cam_children(el, cam):
    """LeadIn / ToolCompensation / MicroConnects of an entity, in CypCut's order (ToolCompensation and MicroConnects
    always present, empty when the source had none)."""
    got = {tag: (attrs, kids) for tag, attrs, kids in cam or []}
    for tag in CAM_TAGS:
        if tag not in got and tag == 'LeadIn':
            continue
        attrs, kids = got.get(tag, ({}, []))
        c = ET.SubElement(el, tag, attrs)
        for ktag, kattrs in kids:
            ET.SubElement(c, ktag, kattrs)


def _part_entities(shapes, entities, mark_outer=True):
    """Kept contours + the other shapes chained into plain ones (no cutting attributes). CypCut tells an outer contour
    from a hole by IsFill (owner's report 2026-11-23: without it the outer contour was cut as a hole, so its lead-in
    and compensation went to the wrong side). When no contour carries it (a DXF), every closed contour whose box lies
    inside no other contour's box gets it."""
    out = [dict(e, attrs=dict(e.get('attrs') or {})) for e in entities or []]
    for it in chain(shapes):
        if it[0] == 'circle':
            out.append({'kind': 'circle', 'cx': it[1], 'cy': it[2], 'r': it[3], 'attrs': {}, 'cam': []})
        else:
            out.append({'kind': 'poly', 'pts': it[1], 'closed': it[2], 'attrs': {}, 'cam': []})
    if mark_outer and not any(e['attrs'].get('IsFill') == 'true' for e in out):
        boxes = [bbox(_entity_shapes(e)) if e['kind'] == 'circle' or e['closed'] else None for e in out]
        tol = 1e-6
        for e, b in zip(out, boxes):
            if b and not any(o is not None and o is not b and o[0] - tol <= b[0] and o[1] - tol <= b[1]
                             and b[2] <= o[2] + tol and b[3] <= o[3] + tol and o != b for o in boxes):
                e['attrs']['IsFill'] = 'true'
    return out


def _pieces_entities(pieces, mark_outer=True):
    """parse() pieces [(entity|None, shapes)] -> writable entities (see _part_entities)."""
    return _part_entities([s for e, sh in pieces if e is None for s in sh], [e for e, _ in pieces if e is not None],
                          mark_outer)


def _rect_entity(w, h):
    return {'kind': 'poly', 'pts': [(0.0, 0.0, 0.0), (w, 0.0, 0.0), (w, h, 0.0), (0.0, h, 0.0)], 'closed': True,
            'attrs': {}, 'cam': []}


def _ents_box(ents):
    return bbox([s for e in ents for s in _entity_shapes(e)])


def _outer_handle(ents, handles):
    return next((h for e, h in zip(ents, handles) if e['attrs'].get('IsFill') == 'true'), handles[0] if handles else 0)


def _new_id():
    import uuid
    return '{' + str(uuid.uuid4()).upper() + '}'


# ---------- old format ----------

def _lxd_entity(parent, ent, handle, extra=None, port=True):
    """One Circle / LwPolyline of an old document, with its cutting attributes."""
    common = {'Handle': str(handle), **({'ChannelPort': '1', 'LayerName': '0'} if port else {}), **(extra or {})}
    a = ent.get('attrs') or {}
    if ent['kind'] == 'circle':
        el = ET.SubElement(parent, 'Circle', {**common, **{k: a[k] for k in ('IsFill',) if k in a},
                                              'Radius': _num(ent['r']),
                                              **({'StartAngle': a['StartAngle']} if 'StartAngle' in a else {})})
        _cam_children(el, ent.get('cam'))
        ET.SubElement(el, 'Center', {'X': _num(ent['cx']), 'Y': _num(ent['cy'])})
        return el
    el = ET.SubElement(parent, 'LwPolyline', {**common, **{k: a[k] for k in ('IsFill', 'PathStartParam') if k in a},
                                              'PointCount': str(len(ent['pts'])),
                                              'PolyFlag': '1' if ent['closed'] else '0'})
    _cam_children(el, ent.get('cam'))
    for x, y, bulge in ent['pts']:
        p = {'X': _num(x), 'Y': _num(y)}
        if abs(bulge) > 1e-12:
            p['Bulge'] = _num(bulge)
        ET.SubElement(el, 'Point', p)
    return el


class _Handles:
    def __init__(self, start):
        self.n = start

    def __call__(self):
        self.n += 1
        return self.n


def _lxd_doc(box, channel, h):
    """The head of an old document: DocHeader, Technical, Viewports, TextStyles, Layers -> (doc, DocHeader)."""
    doc = ET.Element('LXDDocument')
    head = ET.SubElement(doc, 'DocHeader', {'DefaultChannel': '1', 'HandleSeed': '0', 'ID': _new_id()})
    ET.SubElement(head, 'ExtMin', {'X': _num(box[0]), 'Y': _num(box[1])})
    ET.SubElement(head, 'ExtMax', {'X': _num(box[2]), 'Y': _num(box[3])})
    ET.SubElement(head, 'InsertBase')
    ET.SubElement(head, 'HomeRef', {'Enabled': 'true', 'RefType': '3', 'X': _num(box[0]), 'Y': _num(box[1])})
    chans = ET.SubElement(ET.SubElement(doc, 'Technical'), 'Channels')
    ET.SubElement(chans, 'Channel', {'ParamFlag': '256'})
    c = channel or {}
    chans.append(_channel_el(1, c.get('raw'), c.get('fields'), c.get('speed_mm_min'), c.get('gas'),
                             c.get('gas_map')))
    for port in range(2, LXD_EMPTY_PORTS + 1):
        ET.SubElement(chans, 'Channel', {'ChannelPort': str(port)})
    vp = ET.SubElement(ET.SubElement(doc, 'Viewports', {'Handle': '3'}), 'VPort', {'Handle': str(h())})
    ET.SubElement(vp, 'ViewCenter')
    ET.SubElement(vp, 'ViewHeight').text = '100'
    ET.SubElement(vp, 'AspectRatio').text = '1'
    rect = ET.SubElement(vp, 'DisplayRect')
    ET.SubElement(rect, 'Right').text = '1024'
    ET.SubElement(rect, 'Bottom').text = '768'
    for tag, hd in (('TextStyles', '6'), ('Layers', '5')):
        ET.SubElement(doc, tag, {'Handle': hd})
    return doc, head


def _lxd_bytes(doc, head, h):
    head.set('HandleSeed', str(h.n + 1))
    ET.indent(doc, '\t')
    text = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(doc, encoding='unicode') + '\n'
    return text.replace('\n', '\r\n').encode('utf-8')


def write_lxd(shapes, channel=None, entities=None):
    """One part as an old-format .lxd (bytes, CRLF like CypCut). `channel` = {'raw', 'fields', 'speed_mm_min', 'gas'}
    (any may be missing) or None = an empty technology (CypCut uses its own). `entities` = contours kept as the source
    stored them with their lead-in / micro-joints / compensation (parse()['model']['entities']); `shapes` are then the
    other shapes (model['loose']), chained. ValueError without geometry."""
    ents = _part_entities(shapes, entities)
    if not ents:
        raise ValueError('no geometry')
    h = _Handles(1000)
    doc, head = _lxd_doc(_ents_box(ents), channel, h)
    ET.SubElement(doc, 'Blocks', {'Handle': '4'})
    ents_el = ET.SubElement(doc, 'Entities', {'Handle': '2'})
    for ent in ents:
        _lxd_entity(ents_el, ent, h())
    for tag, hd in (('NestParts', '7'), ('NestPlates', '8'), ('NestGroups', '9'), ('NestResults', '10')):
        ET.SubElement(doc, tag, {'Handle': hd})
    return _lxd_bytes(doc, head, h)


def _nest_layout(nest):
    """parse() output of a nest -> writable parts / plates / results (entities, outer contour marked)."""
    parts = []
    for p in nest['parts']:
        ents = _pieces_entities(p.get('pieces') or [])
        if ents:
            parts.append({'name': p['name'] or f'Part{len(parts) + 1}', 'amount': p['amount'], 'used': p['used'],
                          'ents': ents, 'src': p})
    index = {id(p['src']): n for n, p in enumerate(parts)}
    src_parts = nest['parts']
    plates = [{'name': pl.get('name') or 'Rectangle', 'size': pl['size'], 'amount': pl['amount'], 'used': pl['used']}
              for pl in nest['plates'] if pl.get('size')]
    results = []
    for r in nest['results']:
        placed = []
        for pl in r.get('placed') or []:
            ents = _pieces_entities(pl['pieces'])
            if not ents:
                continue
            k = pl['part']
            if k is None:                                           # a placed part no NestPart matched: its own part
                parts.append({'name': f'Part{len(parts) + 1}', 'amount': 1, 'used': 1, 'ents': ents, 'src': None})
                k = len(parts) - 1
            elif id(src_parts[k]) not in index:                     # its NestPart has no drawing: the placed one
                src = src_parts[k]
                index[id(src)] = len(parts)
                parts.append({'name': src['name'] or f'Part{len(parts) + 1}', 'amount': src['amount'] or 1,
                              'used': src['used'], 'ents': ents, 'src': src})
                k = len(parts) - 1
            else:
                k = index[id(src_parts[k])]
            placed.append({'part': k, 'ents': ents})
        extra = _pieces_entities(r.get('extra') or [], mark_outer=False)
        outline = r.get('plate_outline')
        size = r.get('plate')
        if outline is None and size is None and plates:
            size = plates[0]['size']
        if outline is None and size:
            outline = _rect_entity(*size)
        if outline is not None:
            outline = dict(outline, attrs={}, cam=[])
            ob = _ents_box([outline])
            size = (round(ob[2] - ob[0], 3), round(ob[3] - ob[1], 3))
        if size and not any(abs(p['size'][0] - size[0]) < 0.01 and abs(p['size'][1] - size[1]) < 0.01 for p in plates):
            plates.append({'name': 'Rectangle', 'size': size, 'amount': r.get('plate_amount') or 1,
                           'used': r.get('plate_amount') or 1})
        plate = next((n for n, p in enumerate(plates) if size and abs(p['size'][0] - size[0]) < 0.01
                      and abs(p['size'][1] - size[1]) < 0.01), None)
        if placed or extra:
            results.append({'name': r.get('name') or f'Nest result {len(results) + 1}',
                            'utilization': r.get('utilization'), 'plate_amount': r.get('plate_amount') or 1,
                            'placed': placed, 'extra': extra, 'outline': outline, 'plate': plate, 'size': size})
    if not results:
        raise ValueError('no nest result')
    return parts, plates, results


def nest_parts(nest):
    """The parts of a nest one by one (owner's request 2026-11-23: download a part / make a catalog Detail of it):
    [{'name', 'amount', 'ents' (writable, outer contour marked), 'size'}]. ValueError for a file with no result."""
    parts = _nest_layout(nest)[0]
    for p in parts:
        b = _ents_box(p['ents'])
        p['size'] = (round(b[2] - b[0], 3), round(b[3] - b[1], 3))
    return parts


def nest_sheet(nest, result=0):
    """One result of a nest as a cutting-plan sheet (owner's request 2026-11-23): {'parts': [{'name', 'count' (pieces
    on one sheet), 'sig', 'size'}], 'copies' (sheets cut with it), 'size' (sheet w, h), 'results' (how many the file
    has)}. ValueError for a file with no result."""
    parts, _, results = _nest_layout(nest)
    r = results[result]
    counts = {}
    for p in r['placed']:
        counts[p['part']] = counts.get(p['part'], 0) + 1
    out = []
    for n, p in enumerate(parts):
        if counts.get(n):
            shapes = [s for e in p['ents'] for s in _entity_shapes(e)]
            b = bbox(shapes)
            out.append({'name': p['name'], 'count': counts[n], 'sig': _sig(shapes),
                        'size': (round(b[2] - b[0], 1), round(b[3] - b[1], 1))})
    return {'parts': out, 'copies': r['plate_amount'] or 1, 'size': r['size'], 'results': len(results)}


def write_lxd_nest(nest, channel=None):
    """A nest (parse() of an .lxd / .lxds with results) as an old-format .lxd: the parts as Blocks + NestParts, the
    sheets as NestPlates, each result as a NestResult whose Entities are the placed contours (the old format keeps
    them flattened) with their lead-in / micro-joints / compensation + the sheet outline. ValueError without a
    result."""
    parts, plates, results = _nest_layout(nest)
    h = _Handles(1000)
    every = [e for r in results for e in [x for p in r['placed'] for x in p['ents']] + r['extra']
             + ([r['outline']] if r['outline'] else [])]
    doc, head = _lxd_doc(_ents_box(every), channel, h)
    blocks_el = ET.SubElement(doc, 'Blocks', {'Handle': '4'})
    block_names = []
    for n, p in enumerate(parts):
        name = f'Part{n + 1}'
        block_names.append(name)
        b = ET.SubElement(blocks_el, 'Block', {'Handle': str(h()), 'Name': name, 'Flags': '512'})
        box = _ents_box(p['ents'])
        ET.SubElement(b, 'BasePoint', {'X': _num(box[0]), 'Y': _num(box[1])})
        ents_el = ET.SubElement(b, 'Entities', {'Handle': str(h())})
        handles = [h() for _ in p['ents']]
        for ent, hd in zip(p['ents'], handles):
            _lxd_entity(ents_el, ent, hd)
        b.set('OutlineID', str(_outer_handle(p['ents'], handles)))
        p['base'] = box
    ET.SubElement(doc, 'Entities', {'Handle': '2'})
    parts_el = ET.SubElement(doc, 'NestParts', {'Handle': '7'})
    for n, p in enumerate(parts):
        np_el = ET.SubElement(parts_el, 'NestPart', {'Handle': str(h()), 'Name': p['name'], 'Amount': str(p['amount']),
                                                     **({'AmountUsed': str(p['used'])} if p['used'] else {}),
                                                     'ShapeCls': 'Insert'})
        ins = ET.SubElement(np_el, 'Insert', {'Handle': str(h()), 'ChannelPort': '1'})
        ET.SubElement(ins, 'InsertPoint', {'X': _num(p['base'][0]), 'Y': _num(p['base'][1])})
        ET.SubElement(ins, 'BlockName').text = block_names[n]
        for tag in ('XScale', 'YScale', 'ZScale'):
            ET.SubElement(ins, tag).text = '1'
    plates_el = ET.SubElement(doc, 'NestPlates', {'Handle': '8'})
    plate_handles = []
    for pl in plates:
        plate_handles.append(h())
        el = ET.SubElement(plates_el, 'NestPlate', {'Handle': str(plate_handles[-1]), 'Name': pl['name'],
                                                    'Amount': str(pl['amount']), 'AmountUsed': str(pl['used']),
                                                    'ShapeCls': 'LwPolyline'})
        _lxd_entity(el, _rect_entity(*pl['size']), h(), port=False)
    ET.SubElement(doc, 'NestGroups', {'Handle': '9'})
    results_el = ET.SubElement(doc, 'NestResults', {'Handle': '10'})
    for n, r in enumerate(results):
        rh = h()
        if n == 0:
            results_el.set('ActiveModel', str(rh))
        attrs = {'Handle': str(rh), 'Name': r['name'], 'Flags': '704', 'ID': _new_id()}
        if r['utilization'] is not None:
            attrs['Utilization'] = _num(r['utilization'])
        if r['plate_amount'] > 1:
            attrs['PlateAmount'] = str(r['plate_amount'])
        res_el = ET.SubElement(results_el, 'NestResult', attrs)
        ents_el = ET.SubElement(res_el, 'Entities', {'Handle': str(h())})
        for p in r['placed']:
            for ent in p['ents']:
                _lxd_entity(ents_el, ent, h())
        if r['outline'] is not None and r['plate'] is not None:
            _lxd_entity(ents_el, r['outline'], h(), {'NestPlate': str(plate_handles[r['plate']])}, port=False)
        for ent in r['extra']:
            _lxd_entity(ents_el, ent, h())
        box = _ents_box([e for p in r['placed'] for e in p['ents']] + r['extra']
                        + ([r['outline']] if r['outline'] else []))
        ET.SubElement(res_el, 'ExtMin', {'X': _num(box[0]), 'Y': _num(box[1])})
        ET.SubElement(res_el, 'ExtMax', {'X': _num(box[2]), 'Y': _num(box[3])})
        ET.SubElement(res_el, 'HomeRef', {'RefType': '3', 'X': _num(box[0]), 'Y': _num(box[1])})
    return _lxd_bytes(doc, head, h)


# ---------- writing the new format (.lxds) ----------
# Step 2 (owner's decision 2026-11-23, after the .lxd files worked in CypCut): one part as a new-format part file,
# mirroring the part CypCutE 6.4 saved (`Model2D`, no nest), and nests (mirroring CypCut's own nest file).
# Geometry2D/data.bin and Shapes2D/data.bin are built with the record layout of docs/FSCUT_FORMAT.md; each
# content.xml next to a data.bin carries its MD5 (as CypCut writes). Not written: thumbnail.emf,
# GalvoParams/TechFlags (CypCut uses its defaults), remnants.

LXDS_APP_VER = '6.4.2330.6'


def _bin_name(rec, name):
    """'S' 00 u16 len name, padded to 8 relative to the record start (records start 8-aligned in the file)."""
    rec += b'S\x00' + struct.pack('<H', len(name)) + name.encode('ascii')
    rec += b'\x00' * (-len(rec) % 8)
    return rec


def _bin_sub(rec, idx, name, payload, tail=b''):
    """c1c1c1c1 u32 idx u64 size 'S' name payload c0c0c0c0 [tail]; size counts from c1 to c0 inclusive."""
    start = len(rec)
    rec += b'\xc1\xc1\xc1\xc1' + struct.pack('<IQ', idx, 0)
    rec = _bin_name(rec, name) + payload + b'\xc0\xc0\xc0\xc0'
    rec[start + 8:start + 16] = struct.pack('<Q', len(rec) - start)
    return rec + tail


def _bin_record(cls, subs):
    """d1d1d1d1 u32 0 u64 size 'S' TGks<cls> subs d0d0d0d0, padded to 8. subs(rec) appends the sub-records."""
    rec = bytearray(b'\xd1\xd1\xd1\xd1' + struct.pack('<IQ', 0, 0))
    rec = subs(_bin_name(rec, 'TGks' + cls)) + b'\xd0\xd0\xd0\xd0'
    rec[8:16] = struct.pack('<Q', len(rec))
    return bytes(rec + b'\x00' * (-len(rec) % 8))


def _geo_record(ent):
    if ent['kind'] == 'circle':
        start = _f((ent.get('attrs') or {}).get('StartAngle'))
        payload = POINT_TAG + struct.pack('<3dIdd', ent['cx'], ent['cy'], ent['r'], 0, start, 0)
        return _bin_record('Circle', lambda r: _bin_sub(r, 1, 'Circle', payload))
    pts = ent['pts']
    payload = struct.pack('<dII', _f((ent.get('attrs') or {}).get('PathStartParam')), 1 if ent['closed'] else 0, len(pts))
    payload += b''.join(struct.pack('<I3dI', i + 1, x, y, b, 0) for i, (x, y, b) in enumerate(pts)) + b'\x00' * 4
    return _bin_record('LwPolyline', lambda r: _bin_sub(r, 1, 'LwPolyline', payload))


def _attr_record(ent, handle):
    """Shapes2D record: Object (handle), Shape, u32 lead count, [LeadParam], u32 0, Curve (compensation)."""
    cam = {tag: attrs for tag, attrs, _ in ent.get('cam') or []}
    lead, comp = cam.get('LeadIn'), cam.get('ToolCompensation') or {}
    cls = 'Circle' if ent['kind'] == 'circle' else 'LwPolyline'

    def subs(r):
        r = _bin_sub(r, 1, 'Object', struct.pack('<I', handle))
        r = _bin_sub(r, 8, 'Shape', struct.pack('<I56x', 1))
        r += struct.pack('<I', 1 if lead is not None else 0)
        if lead is not None:
            r = _bin_sub(r, 1, 'LeadParam', struct.pack(
                '<I12x5d', int(_f(lead.get('LeadType'))), _f(lead.get('Pos')), _f(lead.get('Angle')),
                _f(lead.get('Length')), _f(lead.get('ArcRadius')), _f(lead.get('LeadHoleR'))))
        r += struct.pack('<I', 0)
        fill = 1 if (ent.get('attrs') or {}).get('IsFill') == 'true' else 0
        return _bin_sub(r, 2, 'Curve', struct.pack('<IIIId12x', fill, 0, 1, int(_f(comp.get('Style'))),
                                                      _f(comp.get('Size'))))
    return _bin_record(cls, subs)


class _ShapeStore:
    """The shapes of a new-format document: Geometry2D + Shapes2D records and the Shapes2D/content.xml list."""

    def __init__(self, h):
        self.h = h
        self.geo, self.attr = bytearray(4096), bytearray(4096)
        self.xml = ET.Element('Shapes2D')

    def add(self, ent, layer=True):
        """-> (handle, class) of a new shape."""
        handle = self.h()
        cls = 'Circle' if ent['kind'] == 'circle' else 'LwPolyline'
        ga, da = len(self.geo), len(self.attr)
        self.geo += _geo_record(ent)
        self.attr += _attr_record(ent, handle)
        el = ET.SubElement(self.xml, cls, {'Handle': str(handle), **({'LayerName': '0'} if layer else {}),
                                           'DataAddr': str(da), 'GeoAddr': str(ga)})
        micro = next((kids for tag, _, kids in ent.get('cam') or [] if tag == 'MicroConnects'), [])
        if micro:
            mc = ET.SubElement(el, 'MicroConnects')
            for _, attrs in micro:
                ET.SubElement(ET.SubElement(mc, 'MicroConnect', attrs), 'LeadIn')
        return handle, cls

    def files(self):
        import hashlib
        for buf in (self.geo, self.attr):
            buf[0:12] = b'BCMP' + struct.pack('<II', len(buf), 1)
        geo, attr = bytes(self.geo), bytes(self.attr)
        ET.SubElement(self.xml, 'MD5').text = hashlib.md5(attr).hexdigest().upper()
        geo_el = ET.Element('Geometry2D')
        ET.SubElement(geo_el, 'MD5').text = hashlib.md5(geo).hexdigest().upper()
        return [('Shapes2D/', None), ('Shapes2D/data.bin', attr), ('Shapes2D/content.xml', _xml_bytes(self.xml)),
                ('Geometry2D/', None), ('Geometry2D/data.bin', geo), ('Geometry2D/content.xml', _xml_bytes(geo_el))]


def _xml_bytes(el):
    ET.indent(el, '\t')
    text = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(el, encoding='unicode') + '\n'
    return text.replace('\n', '\r\n').encode('utf-8')


def _lxds_technical(channel, material, thickness):
    tech = ET.Element('Technical', {'Handle': '2', **({'MaterialName': material} if material else {}),
                                    **({'Thickness': _num(thickness)} if thickness else {})})
    chans = ET.SubElement(tech, 'Channels')
    ET.SubElement(chans, 'Channel', {'ParamFlag': '256'})
    c = channel or {}
    chans.append(_channel_el(1, c.get('raw'), c.get('fields'), c.get('speed_mm_min'), c.get('gas'),
                             c.get('gas_map')))
    for port in range(2, LXD_EMPTY_PORTS + 1):
        ET.SubElement(chans, 'Channel', {'ChannelPort': str(port)})
    return tech


def _lxds_zip(store, h, active_id, technical, name=None, model=None, blocks=None, parts=None, plates=None,
              results=(), counts=(0, 0, 0)):
    """The ZIP of a new-format document, entries in CypCut's order. results = [(index, summary attrs, content el)]."""
    import io
    import time
    pack = ET.Element('PackData', {'DocType': 'Model2D'})
    head = ET.SubElement(pack, 'Header', {'DefaultChannel': '1', 'HandleSeed': str(h.n + 1), 'ID': _new_id(),
                                          'Version': '419963393', **({'PortionName': name} if name else {}),
                                          'OrgAppName': 'CypCutE', 'OrgAppVer': LXDS_APP_VER})
    ET.SubElement(head, 'InsertBase')
    for tag, hd in (('Technical', '2'), ('Viewports', '3'), ('Layers', '5'), ('TextStyles', '6'), ('Blocks', '4')):
        ET.SubElement(pack, tag, {'Handle': hd})
    ET.SubElement(pack, 'Portions', {'Handle': '12', 'ActiveModelID': active_id})
    info = ET.Element('PackData')
    ET.SubElement(info, 'DocVersion', {'Major': '1'})
    ET.SubElement(info, 'Application', {'AppName': 'CypCutE', 'AppVer': LXDS_APP_VER})
    ET.SubElement(info, 'SavedBy', {'SaveTime': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    vports = ET.Element('Viewports', {'Handle': '3', 'Active': '1'})
    vp = ET.SubElement(vports, 'VPort', {'Handle': '1001', 'Name': '1'})
    ET.SubElement(vp, 'ViewCenter')
    for tag, val in (('ZoomCoeff', '1'), ('ViewHeight', '100'), ('AspectRatio', '1')):
        ET.SubElement(vp, tag).text = val
    rect = ET.SubElement(vp, 'DisplayRect')
    ET.SubElement(rect, 'Right').text = '1024'
    ET.SubElement(rect, 'Bottom').text = '768'
    nest = ET.Element('NestInfo')
    ET.SubElement(nest, 'Parts', {'Count': str(counts[0])})
    ET.SubElement(nest, 'NestGroups')
    ET.SubElement(nest, 'Plates', {'Count': str(counts[1])} if counts[1] else {})
    ET.SubElement(nest, 'Results', {'Count': str(counts[2])} if counts[2] else {})
    files = [('info.xml', _xml_bytes(info)), ('content.xml', _xml_bytes(pack))]
    if results:
        summary = ET.Element('NestResults')
        for _, attrs, _ in results:
            ET.SubElement(summary, 'NestResult', attrs)
        files += [('Nest2D/Results/', None), ('Nest2D/Results/content.xml', _xml_bytes(summary))]
    if model is not None:
        files += [('Model2D/', None), ('Model2D/content.xml', _xml_bytes(model))]
    sh = store.files()
    files += sh[:3]
    files += [('TextStyles/', None), ('TextStyles/content.xml', _xml_bytes(ET.Element('TextStyles', {'Handle': '6'}))),
              ('Blocks/', None), ('Blocks/content.xml', _xml_bytes(blocks if blocks is not None else ET.Element('Blocks'))),
              ('Technical/', None), ('Technical/content.xml', _xml_bytes(technical)),
              ('Layers/', None), ('Layers/content.xml', _xml_bytes(ET.Element('Layers', {'Handle': '5'}))),
              ('Nest2D/Parts/', None),
              ('Nest2D/Parts/content.xml', _xml_bytes(parts if parts is not None else ET.Element('Parts')))]
    if plates is not None:
        files += [('Nest2D/Plates/', None), ('Nest2D/Plates/content.xml', _xml_bytes(plates))]
    files += [('Nest2D/', None), ('Nest2D/content.xml', _xml_bytes(nest))]
    for index, _, el in results:
        files += [(f'Nest2D/Results/{index}/', None), (f'Nest2D/Results/{index}/content.xml', _xml_bytes(el))]
    files += [('Viewports/', None), ('Viewports/content.xml', _xml_bytes(vports))] + sh[3:]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as z:
        for fname, data in files:
            z.writestr(fname, data if data is not None else b'')
    return buf.getvalue()


def write_lxds(shapes, channel=None, entities=None, name='Part', material=None, thickness=None):
    """One part as a new-format .lxds (ZIP bytes). Arguments as write_lxd(); `name` = the part's name in CypCut,
    `material` / `thickness` = Technical's MaterialName / Thickness (left out when None). ValueError without geometry."""
    ents = _part_entities(shapes, entities)
    if not ents:
        raise ValueError('no geometry')
    h = _Handles(1100)
    store = _ShapeStore(h)
    b = _ents_box(ents)
    model_id = _new_id()
    model = ET.Element('Model2D', {'Handle': '13', 'Name': name, 'ID': model_id})
    ET.SubElement(model, 'ExtMin', {'X': _num(b[0]), 'Y': _num(b[1])})
    ET.SubElement(model, 'ExtMax', {'X': _num(b[2]), 'Y': _num(b[3])})
    ET.SubElement(model, 'HomeRef', {'RefType': '515', 'X': _num(b[0]), 'Y': _num(b[1])})
    mshapes = ET.SubElement(model, 'Shapes')
    for e in ents:
        handle, cls = store.add(e)
        el = store.xml[-1]
        ET.SubElement(mshapes, 'Shape', {'Class': cls, 'Handle': str(handle), 'DataAddr': el.get('DataAddr'),
                                         'GeoAddr': el.get('GeoAddr')})
    return _lxds_zip(store, h, model_id, _lxds_technical(channel, material, thickness), name=name, model=model)


def write_lxds_nest(nest, channel=None, material=None, thickness=None):
    """A nest (parse() of an .lxd / .lxds with results) as a new-format .lxds, mirroring CypCut's own nest file:
    every part = a Block of its shapes + a NestPart (Insert of that block); every sheet = a NestPlate; every result =
    Nest2D/Results/<n>/content.xml listing the sheet outline and one Insert per placed part, whose {GUID} block holds
    the part already placed (sheet coordinates) with its lead-in / micro-joints / compensation. ValueError without a
    result."""
    parts, plates, results = _nest_layout(nest)
    h = _Handles(1100)
    store = _ShapeStore(h)
    blocks = ET.Element('Blocks')

    def block(name, ents, flags=True):
        b = ET.SubElement(blocks, 'Block', {'Handle': str(h()), 'Name': name, **({'Flags': '512'} if flags else {})})
        ET.SubElement(b, 'BasePoint')
        shapes_el = ET.SubElement(b, 'Shapes', {'Handle': str(h())})
        handles = []
        for e in ents:
            handle, cls = store.add(e)
            handles.append(handle)
            ET.SubElement(shapes_el, 'Shape', {'Class': cls, 'Handle': str(handle)})
        b.set('OutlineID', str(_outer_handle(ents, handles)))
        keys = list(b.attrib)                                   # CypCut's attribute order: Handle Name OutlineID Flags
        order = ['Handle', 'Name', 'OutlineID', 'Flags']
        b.attrib = {k: b.attrib[k] for k in sorted(keys, key=order.index)}

    def insert(parent, tag, handle, block_name, extra=None):
        el = ET.SubElement(parent, tag, {**({'Class': 'Insert'} if tag == 'Shape' else {}), 'Handle': str(handle),
                                         'ChannelPort': '1', **(extra or {})})
        ET.SubElement(el, 'BlockName').text = block_name
        ET.SubElement(el, 'XScale').text = '1'
        ET.SubElement(el, 'YScale').text = '1'
        return el

    parts_el = ET.Element('Parts')
    part_handles = []
    for p in parts:
        name = _new_id()
        block(name, p['ents'])
        part_handles.append(h())
        np_el = ET.SubElement(parts_el, 'NestPart', {'Handle': str(part_handles[-1]), 'Name': p['name'],
                                                     'Amount': str(p['amount']), 'AmountUsed': str(p['used']),
                                                     'GUID': _new_id(), 'ShapeCls': 'Insert'})
        ih = h()
        insert(np_el, 'Insert', ih, name)
        insert(np_el, 'Shape', ih, name)
        ET.SubElement(np_el, 'ExtShapes')
    plates_el = ET.Element('Plates')
    plate_handles = []
    for pl in plates:
        plate_handles.append(h())
        el = ET.SubElement(plates_el, 'NestPlate', {'Handle': str(plate_handles[-1]), 'Name': pl['name'],
                                                    'Amount': str(pl['amount']), 'AmountUsed': str(pl['used']),
                                                    'GUID': _new_id(), 'ShapeCls': 'LwPolyline'})
        rect = _rect_entity(*pl['size'])
        sh_handle, _ = store.add(rect, layer=False)
        poly = ET.SubElement(el, 'LwPolyline', {'Handle': str(sh_handle), 'NestPlate': str(plate_handles[-1]),
                                                'PointCount': '4', 'PolyFlag': '1'})
        ET.SubElement(poly, 'ToolCompensation')
        for x, y, _ in rect['pts']:
            ET.SubElement(poly, 'Point', {k: _num(v) for k, v in (('X', x), ('Y', y)) if v})
        ET.SubElement(el, 'Shape', {'Class': 'LwPolyline', 'Handle': str(sh_handle),
                                    'NestPlate': str(plate_handles[-1])})
        ET.SubElement(el, 'ExtShapes')
    out_results = []
    for n, r in enumerate(results, 1):
        rid = _new_id()
        el = ET.Element('NestResult', {'Handle': str(h()), 'Name': r['name'], 'ID': rid, 'Flags': '64',
                                       'PlateAmount': str(r['plate_amount']),
                                       **({'Utilization': _num(r['utilization'])} if r['utilization'] is not None
                                          else {})})
        box = _ents_box([e for p in r['placed'] for e in p['ents']] + r['extra']
                        + ([r['outline']] if r['outline'] else []))
        ET.SubElement(el, 'ExtMin', {k: _num(v) for k, v in (('X', box[0]), ('Y', box[1])) if v})
        ET.SubElement(el, 'ExtMax', {'X': _num(box[2]), 'Y': _num(box[3])})
        ET.SubElement(el, 'HomeRef', {'RefType': '515', **{k: _num(v) for k, v in (('X', box[0]), ('Y', box[1])) if v}})
        shapes_el = ET.SubElement(el, 'Shapes')
        if r['outline'] is not None and r['plate'] is not None:
            oh, _ = store.add(r['outline'], layer=False)
            ET.SubElement(shapes_el, 'Shape', {'Class': 'LwPolyline', 'Handle': str(oh),
                                               'NestPlate': str(plate_handles[r['plate']])})
        for p in r['placed']:
            name = _new_id()
            block(name, p['ents'], flags=False)
            insert(shapes_el, 'Shape', h(), name, {'NestPart': str(part_handles[p['part']]), 'Selected': 'true'})
        for e in r['extra']:
            eh, cls = store.add(e)
            ET.SubElement(shapes_el, 'Shape', {'Class': cls, 'Handle': str(eh)})
        summary = {'Index': str(n), 'Name': r['name'], 'PartCount': str(len(r['placed'])),
                   **({'Utilization': _num(r['utilization'])} if r['utilization'] is not None else {}),
                   'PlanCount': str(r['plate_amount']),
                   **({'SizeX': _num(r['size'][0]), 'SizeY': _num(r['size'][1])} if r['size'] else {}), 'ID': rid}
        out_results.append((n, summary, el))
    return _lxds_zip(store, h, out_results[0][1]['ID'], _lxds_technical(channel, material, thickness),
                     blocks=blocks, parts=parts_el, plates=plates_el, results=out_results,
                     counts=(len(parts), len(plates), len(results)))


# ---------- the rest of a sheet ("остатък") ----------
# Owner's decision 2026-11-23: the remnant = the sheet minus every placed part's outer contour grown by a gap
# (default 10 mm); one hole-free outline so it can go into CypCut as a plate (DXF or an FSCUT NestPlate). Needs
# shapely (imported here only, so parsing works without it).

ARC_STEP_DEG = 5


def _entity_points(ent):
    """A closed contour as a point ring (arcs / circles flattened every ARC_STEP_DEG)."""
    if ent['kind'] == 'circle':
        n = max(12, int(360 / ARC_STEP_DEG))
        return [(ent['cx'] + ent['r'] * math.cos(2 * math.pi * i / n), ent['cy'] + ent['r'] * math.sin(2 * math.pi * i / n))
                for i in range(n)]
    pts, out = ent['pts'], []
    for i, (x, y, b) in enumerate(pts):
        out.append((x, y))
        if abs(b) > 1e-12 and (ent['closed'] or i < len(pts) - 1):
            x2, y2, _ = pts[(i + 1) % len(pts)]
            arc = _bulge_shape((x, y), (x2, y2), b)
            if arc:
                a1, a2 = arc['start_angle'], arc['end_angle']
                sweep = (a2 - a1) % 360 or 360
                steps = max(1, int(sweep / ARC_STEP_DEG))
                mids = [math.radians(a1 + sweep * k / steps) for k in range(1, steps)]
                ring = [(arc['cx'] + arc['r'] * math.cos(t), arc['cy'] + arc['r'] * math.sin(t)) for t in mids]
                out += ring if b > 0 else ring[::-1]
    return out


def _largest_rect(poly, step):
    """Largest axis-aligned rectangle (w, h) inside `poly`, on a grid of `step` mm (cells whose centre lies in the
    polygon shrunk by half a cell) - histogram method, O(cells)."""
    import numpy as np
    import shapely
    inner = poly.buffer(-step / 2)
    if inner.is_empty:
        return 0.0, 0.0
    x0, y0, x1, y1 = poly.bounds
    xs = np.arange(x0 + step / 2, x1, step)
    ys = np.arange(y0 + step / 2, y1, step)
    gx, gy = np.meshgrid(xs, ys)
    mask = shapely.contains_xy(inner, gx, gy)
    heights = np.zeros(len(xs), dtype=int)
    best = (0, 0, 0)
    for row in mask:
        heights = np.where(row, heights + 1, 0)
        stack = []
        for i, hgt in enumerate(list(heights) + [0]):
            start = i
            while stack and stack[-1][1] >= hgt:
                start, top = stack.pop()
                if top * (i - start) > best[0]:
                    best = (top * (i - start), i - start, top)
            stack.append((start, hgt))
    return round(float(best[1] * step), 1), round(float(best[2] * step), 1)


def nest_remnant(nest, result=0, gap=10.0):
    """The rest of a nest result's sheet: {'pts' [(x, y)] (one hole-free ring), 'area_m2', 'size' (w, h of its box),
    'rect' (w, h of the largest rectangle inside, for a stock remnant lot)} or None when nothing is left. The sheet =
    the result's outline (or its plate size at the origin); every placed part's outer contours (IsFill, else every
    closed one) grown by `gap` mm are taken out; the largest free region is kept, its holes opened to the outline by
    0.1 mm slits (a plate is one contour). ValueError for a file without results."""
    from shapely.geometry import Polygon, box
    from shapely.ops import nearest_points, unary_union
    from shapely.geometry import LineString
    parts, plates, results = _nest_layout(nest)
    r = results[result]
    if r['outline'] is not None:
        sheet = Polygon(_entity_points(r['outline'])).buffer(0)
    elif r['size']:
        sheet = box(0, 0, r['size'][0], r['size'][1])
    else:
        raise ValueError('no sheet')
    used = []
    for p in r['placed']:
        closed = [e for e in p['ents'] if e['kind'] == 'circle' or e['closed']]
        outer = [e for e in closed if e['attrs'].get('IsFill') == 'true'] or closed
        used += [Polygon(_entity_points(e)).buffer(0) for e in outer if len(_entity_points(e)) >= 3]
    free = sheet.difference(unary_union(used).buffer(max(0.0, gap))) if used else sheet
    polys = [g for g in getattr(free, 'geoms', [free]) if g.geom_type == 'Polygon' and not g.is_empty]
    if not polys:
        return None
    region = max(polys, key=lambda g: g.area).simplify(0.05)
    for _ in range(200):                                    # keyhole: each hole joined to the outline by a slit
        if not region.interiors:
            break
        a, b = nearest_points(Polygon(region.interiors[0]).exterior, region.exterior)
        region = region.difference(LineString([a, b]).buffer(0.05, cap_style=3))   # square ends: cuts through
        if region.geom_type != 'Polygon':
            region = max(region.geoms, key=lambda g: g.area)
    if region.interiors:
        raise ValueError('remnant holes could not be opened')
    if region.area < 100:                                   # under 1 cm2: nothing usable
        return None
    x0, y0, x1, y1 = region.bounds
    step = max(2.0, max(x1 - x0, y1 - y0) / 500)
    return {'pts': [(round(x, 3), round(y, 3)) for x, y in list(region.exterior.coords)[:-1]],
            'area_m2': round(region.area / 1e6, 4), 'size': (round(x1 - x0, 1), round(y1 - y0, 1)),
            'rect': _largest_rect(region, step)}


def _plate_entity(pts):
    return {'kind': 'poly', 'pts': [(x, y, 0.0) for x, y in pts], 'closed': True, 'attrs': {}, 'cam': []}


def write_lxd_plate(pts, name='Остатък'):
    """A remnant as an old-format document whose only content is one NestPlate with that outline."""
    plate = _plate_entity(pts)
    h = _Handles(1000)
    doc, head = _lxd_doc(_ents_box([plate]), None, h)
    for tag, hd in (('Blocks', '4'), ('Entities', '2'), ('NestParts', '7')):
        ET.SubElement(doc, tag, {'Handle': hd})
    plates_el = ET.SubElement(doc, 'NestPlates', {'Handle': '8'})
    el = ET.SubElement(plates_el, 'NestPlate', {'Handle': str(h()), 'Name': name, 'Amount': '1', 'AmountUsed': '0',
                                                'ShapeCls': 'LwPolyline'})
    _lxd_entity(el, plate, h(), port=False)
    for tag, hd in (('NestGroups', '9'), ('NestResults', '10')):
        ET.SubElement(doc, tag, {'Handle': hd})
    return _lxd_bytes(doc, head, h)


def write_lxds_plate(pts, name='Остатък'):
    """A remnant as a new-format document whose only content is one NestPlate with that outline."""
    plate = _plate_entity(pts)
    h = _Handles(1100)
    store = _ShapeStore(h)
    plates_el = ET.Element('Plates')
    ph = h()
    el = ET.SubElement(plates_el, 'NestPlate', {'Handle': str(ph), 'Name': name, 'Amount': '1', 'AmountUsed': '0',
                                                'GUID': _new_id(), 'ShapeCls': 'LwPolyline'})
    sh, _ = store.add(plate, layer=False)
    poly = ET.SubElement(el, 'LwPolyline', {'Handle': str(sh), 'NestPlate': str(ph),
                                            'PointCount': str(len(pts)), 'PolyFlag': '1'})
    ET.SubElement(poly, 'ToolCompensation')
    for x, y in pts:
        ET.SubElement(poly, 'Point', {k: _num(v) for k, v in (('X', x), ('Y', y)) if v})
    ET.SubElement(el, 'Shape', {'Class': 'LwPolyline', 'Handle': str(sh), 'NestPlate': str(ph)})
    ET.SubElement(el, 'ExtShapes')
    return _lxds_zip(store, h, _new_id(), _lxds_technical(None, None, None), plates=plates_el, counts=(0, 1, 0))
