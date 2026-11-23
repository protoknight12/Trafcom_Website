"""
pytest: FSCUT / CypCutE nesting files ("разкрой") kept with a Detail - fscut.py reads the old .lxd (plain XML) and
the new .lxds (ZIP + binary Geometry2D/data.bin) into the same summary; view_detail_nest() shows it (admin only)
with buttons that post the technology / a part's count per sheet to the existing detail routes.
The real sample files are machine files and stay out of git (machine_data/fscut_samples/) - built here by hand
with the record layout documented in docs/FSCUT_FORMAT.md.

Run with:
    pytest testing/test_fscut_nest.py -v
"""
import io
import json
import math
import os
import struct
import xml.etree.ElementTree as ET
import zipfile

import pytest
from flask import g
from werkzeug.security import generate_password_hash

import fscut
from app import (app as flask_app, db, User, MaterialPrice, Detail, DetailDxfFile, Machine, MachineCutTechnology,
                 MaterialGrade, HallMachine, limiter, seed_material_types)

TECH = ('<Technical MaterialName="碳钢" Thickness="8"><Channels><Channel ChannelPort="1">'
        '<CamParams WorkSpeed="50" LiftHeight="30"><Cut GasType="2" GasPressure=".7" PeakPower="6000" Focus="11" '
        'Height=".6" PwmRatio=".5"/>'
        '<Pierce1 GasType="3" Focus="-6" TimeFollow="500" TimeStay="100"/><Pierce2 GasType="2" PwmFreq="200"/>'
        '<SmartEndTech ExtGap=".6"/><MaterialParams NozzleSpec="S2.0/3.0" Thickness="8"/></CamParams></Channel>'
        '<Channel ChannelPort="101"><CamParams WorkSpeed="9"/></Channel></Channels></Technical>')

# old format: one square 100x50 with a 10 mm hole, placed by an Insert at (10, 10)
LXD = ('<?xml version="1.0" encoding="utf-8"?><LXDDocument>' + TECH.replace('Technical MaterialName="碳钢" Thickness="8"', 'Technical') +
       '<Blocks><Block Name="P1"><BasePoint/><Entities>'
       '<LwPolyline PolyFlag="1"><Point/><Point X="100"/><Point X="100" Y="50"/><Point Y="50"/></LwPolyline>'
       '<Circle Radius="5"><Center X="50" Y="25"/></Circle></Entities></Block></Blocks>'
       '<NestParts><NestPart Name="Планка" Amount="2" AmountUsed="1"/></NestParts>'
       '<NestPlates><NestPlate Handle="9" AmountUsed="1"><LwPolyline><Point/><Point X="1000"/><Point X="1000" Y="2000"/>'
       '<Point Y="2000"/></LwPolyline></NestPlate></NestPlates>'
       '<NestResults><NestResult Name="R1" Utilization="12.5"><Entities>'
       '<Insert><InsertPoint X="10" Y="10"/><BlockName>P1</BlockName></Insert>'
       '</Entities></NestResult></NestResults></LXDDocument>')


def _name(s):
    b = b'S\x00' + struct.pack('<H', len(s)) + s.encode()
    return b + b'\x00' * (-len(b) % 8)                 # 'S' sits on an 8-aligned offset, padded to the next one


def _record(cls, payload):
    inner = b'\xc1\xc1\xc1\xc1' + struct.pack('<IQ', 1, 0) + _name(cls) + payload + b'\xc0\xc0\xc0\xc0'
    body = _name('TGks' + cls) + inner + b'\xd0\xd0\xd0\xd0'
    rec = b'\xd1\xd1\xd1\xd1' + struct.pack('<IQ', 0, len(body)) + body
    return rec + b'\x00' * (-len(rec) % 8)


def _pt(x, y):
    return fscut.POINT_TAG + struct.pack('<2d', x, y)


def _attr_record(cls, subs):
    """A Shapes2D/data.bin record: named sub-records (LeadParam, Curve), each c1 ... c0 + u32 child count."""
    body = _name('TGks' + cls)
    for name, payload in subs:
        payload += b'\x00' * (-len(payload) % 8)
        body += b'\xc1\xc1\xc1\xc1' + struct.pack('<IQ', 1, 0) + _name(name) + payload + b'\xc0\xc0\xc0\xc0' + b'\x00' * 4
    rec = b'\xd1\xd1\xd1\xd1' + struct.pack('<IQ', 0, len(body) + 4) + body + b'\xd0\xd0\xd0\xd0'
    return rec + b'\x00' * (-len(rec) % 8)


def _lxds(part=False, cam=False):
    """New format: plate polyline + a placed part made of a closed polyline (100x50, bulge 0) and a circle r=5 +
    a 50 mm line and a half arc, all in Geometry2D/data.bin. cam: the polyline and the circle carry a lead-in
    (LeadParam) and a 0.4 compensation (Curve) in Shapes2D/data.bin, the polyline 2 micro-joints in XML, the circle
    a start angle of 45 degrees - as CypCut saves them."""
    poly = struct.pack('<dII', 0, 1, 4) + b''.join(
        struct.pack('<I3dI', i + 1, x, y, 0, 0) for i, (x, y) in enumerate([(10, 10), (110, 10), (110, 60), (10, 60)]))
    plate = struct.pack('<dII', 0, 1, 4) + b''.join(
        struct.pack('<I3dI', i + 1, x, y, 0, 0) for i, (x, y) in enumerate([(0, 0), (1000, 0), (1000, 2000), (0, 2000)]))
    records = [('LwPolyline', poly), ('Circle', _pt(60, 35) + struct.pack('<dIdd', 5, 0, math.pi / 4 if cam else 0, 0)),
               ('Line', _pt(200, 10) + _pt(250, 10)),
               ('Arc', _pt(300, 10) + struct.pack('<dIdd', 10, 0, 0, math.pi)), ('LwPolyline', plate)]
    geo, addrs = bytearray(b'BCMP' + b'\x00' * 4092), []
    for cls, payload in records:
        addrs.append(len(geo))
        geo += _record(cls, payload)
    attr, extra = bytearray(b'BCMP' + b'\x00' * 4092), {}
    if cam:
        for i, pos in ((0, 1.5), (1, 0.0)):
            extra[i] = f' DataAddr="{len(attr)}"'
            attr += _attr_record(records[i][0], [
                ('LeadParam', struct.pack('<I12x5d', 1, pos, math.pi / 2, 4, 2, .5)),
                ('Curve', struct.pack('<8xIId12x', 1, 1, .4))])
    micro = '<MicroConnects><MicroConnect Size=".4"><LeadIn/></MicroConnect><MicroConnect Pos="2" Size=".4"/></MicroConnects>'
    shapes = ''.join(f'<{cls} Handle="{100 + i}" LayerName="0" GeoAddr="{a}"{extra.get(i, "")}>'
                     f'{micro if cam and i == 0 else ""}</{cls}>'
                     for i, ((cls, _), a) in enumerate(zip(records, addrs)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('info.xml', '<PackData><Application AppName="CypCutE" AppVer="6.4.2330.6"/></PackData>')
        z.writestr('Technical/content.xml', TECH)
        z.writestr('Shapes2D/content.xml', f'<Shapes2D>{shapes}</Shapes2D>')
        z.writestr('Geometry2D/data.bin', bytes(geo))
        if cam:
            z.writestr('Shapes2D/data.bin', bytes(attr))
        z.writestr('Blocks/content.xml', '<Blocks><Block Name="{G1}"><BasePoint/><Shapes>'
                   + ''.join(f'<Shape Handle="{100 + i}"/>' for i in range(4)) + '</Shapes></Block></Blocks>')
        z.writestr('Nest2D/Parts/content.xml', '<Parts><NestPart Handle="7" Name="Планка" Amount="3" AmountUsed="1"/></Parts>')
        z.writestr('Nest2D/Plates/content.xml', '<Plates><NestPlate Handle="9" AmountUsed="1"><LwPolyline><Point/>'
                   '<Point X="1000"/><Point X="1000" Y="2000"/><Point Y="2000"/></LwPolyline></NestPlate></Plates>')
        if part:            # a part file: Model2D lists the part's own shapes, no nest result
            z.writestr('Model2D/content.xml', '<Model2D Name="liceva"><Shapes>'
                       + ''.join(f'<Shape Handle="{100 + i}"/>' for i in range(4)) + '</Shapes></Model2D>')
        else:
            z.writestr('Nest2D/Results/content.xml',
                       '<NestResults><NestResult Index="1" Name="Nest result" Utilization="5"/></NestResults>')
            z.writestr('Nest2D/Results/1/content.xml', '<NestResult PlateAmount="2" Utilization="5"><Shapes>'
                       '<Shape Class="LwPolyline" Handle="104" NestPlate="9"/>'
                       '<Shape Class="Insert" NestPart="7"><BlockName>{G1}</BlockName></Shape></Shapes></NestResult>')
    return buf.getvalue()


def test_parse_both_formats(tmp_path):
    old = tmp_path / 'a.lxd'
    old.write_text(LXD, encoding='utf-8')
    d = fscut.parse(str(old))
    r = d['results'][0]
    assert d['format'] == 'lxd' and r['plate'] == (1000, 2000) and r['utilization'] == 12.5
    assert r['cut_length_mm'] == round(300 + 2 * math.pi * 5, 1) and r['pierces'] == 2
    assert fscut.bbox(r['shapes']) == (10, 10, 110, 60)                  # the Insert moved the block by (10, 10)
    ch = d['technology']['channels']
    assert len(ch) == 1 and ch[0]['speed_mm_min'] == 3000 and ch[0]['gas'] == 'oxygen' and ch[0]['pierce_time_sec'] == 0.6

    new = tmp_path / 'b.lxds'
    new.write_bytes(_lxds())
    d = fscut.parse(str(new))
    r = d['results'][0]
    assert d['format'] == 'lxds' and d['app_version'] == '6.4.2330.6'
    assert fscut.material_name(d['technology']['material']) == 'Черна стомана' and d['technology']['thickness'] == 8
    assert r['counts'] == {'Планка': 1} and r['plate'] == (1000, 2000) and r['plate_amount'] == 2
    assert r['cut_length_mm'] == round(300 + 2 * math.pi * 5 + 50 + math.pi * 10, 1)
    assert r['pierces'] == 4 and len(r['plate_shapes']) == 4
    assert fscut.bbox(r['shapes']) == (10, 10, 310, 60)                  # exact arc extents: upper half to y = 20
    assert d['model'] is None
    arc = [s for s in r['shapes'] if s['type'] == 'arc'][0]
    assert (round(arc['start_angle']), round(arc['end_angle'])) == (0, 180)

    # part files (one detail saved from FSCUT, no nest): new = Model2D, old = the document's own Entities
    new.write_bytes(_lxds(part=True))
    d = fscut.parse(str(new))
    m = d['model']
    assert not d['results'] and m['name'] == 'liceva' and m['size'] == (300, 50) and m['pierces'] == 4
    assert m['cut_length_mm'] == round(300 + 2 * math.pi * 5 + 50 + math.pi * 10, 1)
    old.write_text(LXD.replace('<NestResults>', '<Entities><LwPolyline PolyFlag="1"><Point/><Point X="40"/>'
                                                '<Point X="40" Y="20"/><Point Y="20"/></LwPolyline></Entities><NestResults>'),
                   encoding='utf-8')
    m = fscut.parse(str(old))['model']
    assert m['size'] == (40, 20) and m['cut_length_mm'] == 120 and m['pierces'] == 1

    bad = tmp_path / 'c.lxds'
    bad.write_bytes(b'not a nesting file')
    with pytest.raises(ValueError):
        fscut.parse(str(bad))
    bad.write_bytes(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><LXDDocument>&a;</LXDDocument>')
    with pytest.raises(ValueError):                                      # no DTD / entities accepted
        fscut.parse(str(bad))
    # nested Inserts multiply (100 x 100 x 100 x 100 circles): refused by the per-file budget, not expanded
    level = lambda n, child: (f'<Block Name="B{n}"><Entities>' + f'<Insert><BlockName>B{n + 1}</BlockName></Insert>' * 100
                              + '</Entities></Block>') if child else (f'<Block Name="B{n}"><Entities>'
                              + '<Circle Radius="1"><Center/></Circle>' * 100 + '</Entities></Block>')
    bad.write_text('<LXDDocument><Blocks>' + ''.join(level(n, n < 3) for n in range(4)) + '</Blocks><NestResults>'
                   '<NestResult><Entities><Insert><BlockName>B0</BlockName></Insert></Entities></NestResult>'
                   '</NestResults></LXDDocument>', encoding='utf-8')
    with pytest.raises(ValueError):
        fscut.parse(str(bad))
    # absurd values from the file: no endless loop / crash
    bad.write_text(LXD.replace('LiftHeight="30"', 'LiftHeight="30" PierceStepCount="1e300"')
                   .replace('GasType="2"', 'GasType="1e400"'), encoding='utf-8')
    ch = fscut.parse(str(bad))['technology']['channels'][0]
    assert ch['fields']['pierce_steps'] == 1
    deep = '<CamParams WorkSpeed="1">' + '<X>' * 40 + '</X>' * 40 + '</CamParams>'
    bad.write_text(LXD.replace('<Channel ChannelPort="101"><CamParams WorkSpeed="9"/>',
                               '<Channel ChannelPort="2">' + deep), encoding='utf-8')
    with pytest.raises(ValueError):                                      # technology nested too deep
        fscut.parse(str(bad))


def test_bulge_is_dxf_bulge():
    # bulge 1 = half circle to the left of p1 -> p2 (CCW): (0,0) -> (10,0) bulges down, centre (5, 0)
    a = fscut._bulge_shape((0, 0), (10, 0), 1)
    assert (round(a['cx'], 6), round(a['cy'], 6), round(a['r'], 6)) == (5, 0, 5)
    assert round(fscut.shape_length(a), 6) == round(math.pi * 5, 6)
    assert (round(a['start_angle']) % 360, round(a['end_angle']) % 360) == (180, 0)   # CCW 180 -> 360: lower half


@pytest.fixture
def s():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        pw = generate_password_hash('irrelevant123')
        db.session.add_all([User(username='fn_admin', password=pw, role='admin'),
                            User(username='fn_worker', password=pw, role='worker'),
                            MaterialPrice(key='fn_steel', display_name='Стомана 8', type='sheets', cost_per_m2=10,
                                          sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=8)])
        db.session.commit()
        yield
        db.session.remove()
        db.drop_all()


def _login(username):
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


def _attach(detail, name, data):
    stored = f'test_{os.getpid()}_{name}'
    with open(os.path.join(flask_app.config['DETAIL_DXF_FOLDER'], stored), 'wb') as fh:
        fh.write(data)
    f = DetailDxfFile(detail_id=detail.id, filename=stored, original_filename=name)
    db.session.add(f)
    db.session.commit()
    return f, os.path.join(flask_app.config['DETAIL_DXF_FOLDER'], stored)


def test_nest_page(s):
    d = Detail(name='Планка', material_key='fn_steel', width=100, height=50, total_length=300, pierce_count=2,
               calculated_price=1)
    db.session.add(d)
    db.session.commit()
    f, path = _attach(d, 'разкрой.lxds', _lxds())
    other, other_path = _attach(d, 'чертеж.pdf', b'%PDF-1.4')
    broken, broken_path = _attach(d, 'счупен.lxd', b'<nope')
    try:
        c = _login('fn_admin')
        assert 'Разкрой' in c.get(f'/details/{d.id}/files').get_data(as_text=True)
        page = c.get(f'/details/files/{f.id}/nest').get_data(as_text=True)
        assert 'Черна стомана' in page and '1000 × 2000' in page and '3000' in page and 'Кислород' in page
        assert '1 = детайли от един лист' in page and 'Вземи технологията' in page and 'NEST_SHAPES' in page
        assert c.get(f'/details/files/{other.id}/nest').status_code == 404
        r = c.get(f'/details/files/{broken.id}/nest')
        assert r.status_code == 302 and f'/details/{d.id}/files' in r.headers['Location']

        # the buttons post to the existing routes
        c.post(f'/admin/details/{d.id}/pieces-per-unit', data={'pieces_per_unit': '1'})
        c.post(f'/admin/details/{d.id}/cut-technology',
               data={'machine_id': '', 'cutting_speed_mm_per_min': '3000.0', 'pierce_time_sec': '0.6', 'gas': 'oxygen'})
        db.session.expire_all()
        d = db.session.get(Detail, d.id)
        assert (d.pieces_per_unit, d.cutting_speed_mm_per_min, d.pierce_time_sec, d.cutting_gas) == (1, 3000, 0.6, 'oxygen')

        # a part file gets the same page: its drawing, size, length, no sheet
        part, part_path = _attach(d, 'Лицежа.lxds', _lxds(part=True))
        try:
            page = c.get(f'/details/files/{part.id}/nest').get_data(as_text=True)
            assert 'Детайл FSCUT' in page and 'Детайл във файла: liceva' in page and '300 × 50 мм' in page
            assert 'nest-canvas-1' in page and 'Вземи технологията' in page
        finally:
            os.remove(part_path)

        w = _login('fn_worker')
        assert w.get(f'/details/files/{f.id}/nest').status_code == 302          # admin only
    finally:
        for p in (path, other_path, broken_path):
            os.remove(p)


def test_migration_matches_model():
    """The frozen migration adds exactly the FSCUT columns the model builds from fscut.tech_columns()."""
    src = open(os.path.join(os.path.dirname(__file__), '..', 'migration', 'migrate_add_cut_tech_fscut_fields.py'),
               encoding='utf-8').read()
    for col, *_ in fscut.tech_columns():
        base = col.split('_', 1)[1] if col[:2] in ('p1', 'p2', 'p3') and col[2] == '_' else col
        assert f"'{base}'" in src or col.endswith('_gas'), col
    assert {c for c, *_ in fscut.tech_columns()} <= set(MachineCutTechnology.__table__.columns.keys())


def test_machine_technology_from_nest_and_dossier(s):
    grade = MaterialGrade(name='Стомана FN', density_kg_dm3=7.85)
    laser = Machine(name='Лазер FN', is_laser=True)
    lathe = Machine(name='Струг FN')
    db.session.add_all([grade, laser, lathe])
    db.session.flush()
    hall = HallMachine(name='Лазер FN', machine_id=laser.id)
    db.session.add(hall)
    MaterialPrice.query.filter_by(key='fn_steel').one().grade_id = grade.id
    d = Detail(name='Планка', material_key='fn_steel', width=100, height=50, total_length=300, pierce_count=2,
               calculated_price=1)
    db.session.add(d)
    db.session.commit()
    f, path = _attach(d, 'разкрой.lxds', _lxds())
    try:
        c = _login('fn_admin')
        page = c.get(f'/details/files/{f.id}/nest').get_data(as_text=True)
        assert 'S2.0/3.0' in page and 'Запиши като технология на машината' in page and 'Пробиване 3' in page
        url = f'/details/files/{f.id}/nest/save-tech'
        form = {'machine_id': laser.id, 'port': 1, 'grade_id': grade.id, 'thickness_mm': '8'}
        c.post(url, data=form)
        t = MachineCutTechnology.query.one()
        assert (t.machine_id, t.grade_id, t.thickness_mm, t.gas, t.nozzle) == (laser.id, grade.id, 8, 'oxygen', 'S2.0/3.0')
        assert (t.cutting_speed_mm_per_min, t.pierce_time_sec, t.gas_pressure_bar, t.peak_power_w) == (3000, 0.6, 0.7, 6000)
        assert (t.pwm_ratio_pct, t.lift_height_mm, t.pierce_steps, t.p1_gas, t.p1_focus_mm) == (50, 30, 1, 'nitrogen', -6)
        assert (t.p2_gas, t.p2_freq_hz, t.p3_gas, t.p3_freq_hz) == ('oxygen', 200, None, None)
        assert t.beam_size_mm is None                               # not in the file = unknown, not 0
        assert t.source == 'разкрой.lxds' and any(p.endswith('SmartEndTech') for p, *_ in json.loads(t.params_json))
        c.post(url, data=form)                                      # same key: overwritten, not added
        assert MachineCutTechnology.query.count() == 1
        r = c.post(url, data={**form, 'machine_id': lathe.id})
        assert MachineCutTechnology.query.count() == 1 and r.status_code == 302     # not a laser: refused

        # the dossier lists it with everything and edits every field; another nozzle = another row
        dossier = c.get(f'/admin/hall/{hall.id}/dossier').get_data(as_text=True)
        assert 'S2.0/3.0' in dossier and 'разкрой.lxds' in dossier and 'SmartEndTech' in dossier
        save = f'/admin/hall/{hall.id}/cut-tech'
        edit = {'tech_id': t.id, 'grade_id': grade.id, 'thickness_mm': '8', 'gas': 'oxygen', 'nozzle': 'S2.0/3.0',
                'cutting_speed_mm_per_min': '2500', 'p1_focus_mm': '-5', 'p2_gas': 'air', 'peak_power_w': '4000'}
        c.post(save, data=edit)
        db.session.expire_all()
        t = db.session.get(MachineCutTechnology, t.id)
        assert (t.cutting_speed_mm_per_min, t.p1_focus_mm, t.p2_gas, t.peak_power_w, t.p1_gas) == (2500, -5, 'air', 4000, None)
        assert t.params_json                                         # the file's parameters stay with the row
        c.post(save, data={**edit, 'peak_power_w': '-1'})            # negative power refused
        db.session.expire_all()
        assert db.session.get(MachineCutTechnology, t.id).peak_power_w == 4000
        c.post(save, data={**edit, 'tech_id': '', 'nozzle': '1.2'})
        assert MachineCutTechnology.query.count() == 2

        # the detail page: picking the machine offers both, saving with a nozzle hits that row only
        page = c.get(f'/details/{d.id}/files').get_data(as_text=True)
        assert '"nozzle": "1.2"' in page and '"nozzle": "S2.0/3.0"' in page
        c.post(f'/admin/details/{d.id}/cut-technology', data={
            'machine_id': laser.id, 'gas': 'oxygen', 'nozzle': '1.2', 'cutting_speed_mm_per_min': '1800',
            'save_to_machine': '1'})
        db.session.expire_all()
        assert db.session.get(Detail, d.id).cutting_nozzle == '1.2'
        assert sorted((x.nozzle, x.cutting_speed_mm_per_min) for x in MachineCutTechnology.query) == \
               [('1.2', 1800), ('S2.0/3.0', 2500)]

        assert _login('fn_worker').post(url, data=form).status_code == 302
        assert MachineCutTechnology.query.count() == 2
    finally:
        os.remove(path)


def test_write_lxd_round_trip(tmp_path):
    # a 100 x 50 plate with one rounded corner, drawn as loose lines/arc in mixed directions, + a hole
    shapes = [{'type': 'line', 'x1': 0, 'y1': 0, 'x2': 100, 'y2': 0},
              {'type': 'line', 'x1': 100, 'y1': 40, 'x2': 100, 'y2': 0},                      # reversed piece
              {'type': 'arc', 'cx': 90, 'cy': 40, 'r': 10, 'start_angle': 0, 'end_angle': 90},
              {'type': 'line', 'x1': 90, 'y1': 50, 'x2': 0, 'y2': 50},
              {'type': 'line', 'x1': 0, 'y1': 50, 'x2': 0, 'y2': 0},
              {'type': 'circle', 'cx': 30, 'cy': 25, 'r': 5}]
    polys = [i for i in fscut.chain(shapes) if i[0] == 'poly']
    assert len(polys) == 1 and polys[0][2] is True                       # one closed contour
    tech = fscut.parse(str(_write(tmp_path / 't.lxds', _lxds(part=True))))['technology']['channels'][0]
    p = tmp_path / 'out.lxd'
    p.write_bytes(fscut.write_lxd(shapes, tech))
    assert p.read_bytes().startswith(b'<?xml') and b'\r\n' in p.read_bytes()
    d = fscut.parse(str(p))
    m, ch = d['model'], d['technology']['channels'][0]
    assert d['format'] == 'lxd' and m['size'] == (100, 50) and m['pierces'] == 2
    assert m['cut_length_mm'] == round(100 + 40 + math.pi * 5 + 90 + 50 + 2 * math.pi * 5, 1)
    assert ch['fields'] == tech['fields'] and ch['speed_mm_min'] == tech['speed_mm_min'] and ch['nozzle'] == 'S2.0/3.0'
    assert any(r[0].endswith('SmartEndTech') for r in ch['raw'])          # the whole tree went along
    # edited values win over the stored tree; no technology = an empty channel
    edited = dict(tech, fields={**tech['fields'], 'peak_power_w': 4000, 'p1_gas': 'air'}, speed_mm_min=1200)
    ch = fscut.parse(_write(tmp_path / 'e.lxd', fscut.write_lxd(shapes, edited)))['technology']['channels'][0]
    assert (ch['fields']['peak_power_w'], ch['fields']['p1_gas'], ch['speed_mm_min']) == (4000, 'air', 1200)
    assert fscut.parse(_write(tmp_path / 'n.lxd', fscut.write_lxd(shapes)))['model']['pierces'] == 2
    with pytest.raises(ValueError):
        fscut.write_lxd([])


def _write(path, data):
    path.write_bytes(data)
    return str(path)


CAM_LXD = ('<?xml version="1.0" encoding="utf-8"?><LXDDocument><Entities>'
           '<Circle Radius="5" StartAngle=".785398163"><LeadIn LeadType="1" Angle="1.57079633" Length="4" ArcRadius="2" '
           'LeadHoleR=".5"/><ToolCompensation Style="1" Size=".4"/><MicroConnects/><Center X="60" Y="35"/></Circle>'
           '<LwPolyline PathStartParam="1.5" PolyFlag="1"><LeadIn LeadType="1" Pos="1.5" Angle="1.57079633" Length="4" '
           'ArcRadius="2" LeadHoleR=".5"/><ToolCompensation Style="1" Size=".4"/><MicroConnects><MicroConnect Size=".4"/>'
           '<MicroConnect Pos="2" Size=".4"/></MicroConnects><Point X="10" Y="10"/><Point X="110" Y="10"/>'
           '<Point X="110" Y="60"/><Point X="10" Y="60"/></LwPolyline></Entities></LXDDocument>')


def test_write_lxd_keeps_lead_in_micro_joints_compensation(tmp_path):
    """A part converted to .lxd keeps the source's lead-ins, micro-joints and compensation (old: XML, new: Shapes2D
    data.bin + XML micro-joints), on the contour exactly as stored so the Pos values still point at the same spot."""
    want = {('Circle', 'StartAngle', '.785398163'), ('LwPolyline', 'PathStartParam', '1.5'),
            ('LeadIn', 'Pos', '1.5'), ('LeadIn', 'Length', '4'), ('LeadIn', 'ArcRadius', '2'),
            ('LeadIn', 'LeadHoleR', '.5'), ('LeadIn', 'Angle', '1.57079633'),
            ('ToolCompensation', 'Style', '1'), ('ToolCompensation', 'Size', '.4'), ('MicroConnect', 'Pos', '2')}
    for name, data in (('old.lxd', CAM_LXD.encode()), ('new.lxds', _lxds(part=True, cam=True))):
        m = fscut.parse(_write(tmp_path / name, data))['model']
        out = fscut.write_lxd(m['loose'], None, m['entities'])
        ents = ET.fromstring(out).find('Entities')
        got = {(e.tag, k, v) for e in ents.iter() for k, v in e.attrib.items()}
        assert want <= got, (name, want - got)
        assert len(ents.findall('.//LeadIn')) == 2 and len(ents.findall('.//MicroConnect')) == 2
        assert not ents.findall('.//MicroConnect/LeadIn')           # the new format's nested LeadIn is not written
        back = fscut.parse(_write(tmp_path / (name + '.lxd'), out))['model']
        assert (back['size'], back['pierces'], back['cut_length_mm']) == (m['size'], m['pierces'], m['cut_length_mm'])
        pts = [(p.get('X'), p.get('Y')) for p in ents.find('LwPolyline').findall('Point')]
        assert pts == [('10', '10'), ('110', '10'), ('110', '60'), ('10', '60')]    # vertex order kept
    # a part with no such data: empty ToolCompensation / MicroConnects, no LeadIn (as before)
    m = fscut.parse(_write(tmp_path / 'plain.lxds', _lxds(part=True)))['model']
    ents = ET.fromstring(fscut.write_lxd(m['loose'], None, m['entities'])).find('Entities')
    assert not ents.findall('.//LeadIn') and all(not t.attrib for t in ents.iter('ToolCompensation'))


def test_fscut_download_for_machine(s, tmp_path):
    laser = Machine(name='Лазер стар', is_laser=True)
    db.session.add(laser)
    db.session.flush()
    hall = HallMachine(name='Лазер стар', machine_id=laser.id)
    db.session.add(hall)
    shapes = [{'type': 'line', 'x1': 0, 'y1': 0, 'x2': 100, 'y2': 0}, {'type': 'line', 'x1': 100, 'y1': 0, 'x2': 100, 'y2': 50},
              {'type': 'line', 'x1': 100, 'y1': 50, 'x2': 0, 'y2': 50}, {'type': 'line', 'x1': 0, 'y1': 50, 'x2': 0, 'y2': 0}]
    d = Detail(name='Планка/1', material_key='fn_steel', width=100, height=50, total_length=300, pierce_count=1,
               calculated_price=1, geometry_json=json.dumps(shapes), cutting_gas='nitrogen', cutting_nozzle='1.5',
               cutting_speed_mm_per_min=900)
    db.session.add(d)
    db.session.commit()
    c = _login('fn_admin')
    url = f'/admin/details/{d.id}/fscut?machine_id={laser.id}'
    r = c.get(url)                                                     # format not set: a message, no file
    assert r.status_code == 302 and 'FSCUT' in c.get(r.headers['Location']).get_data(as_text=True)
    c.post(f'/admin/hall/{hall.id}/dossier/system', data={'status': 'idle', 'is_laser': '1', 'fscut_format': 'lxds'})
    assert db.session.get(Machine, laser.id).fscut_format == 'lxds'
    assert c.get(url).status_code == 302                                 # new format: not written yet
    c.post(f'/admin/hall/{hall.id}/dossier/system', data={'status': 'idle', 'is_laser': '1', 'fscut_format': 'lxd'})
    r = c.get(url)                                                     # no machine technology: the detail's speed + gas
    assert r.status_code == 200 and '.lxd' in r.headers['Content-Disposition'] and '/' not in r.headers['Content-Disposition']
    ch = fscut.parse(_write(tmp_path / 'a.lxd', r.data))['technology']['channels'][0]
    assert (ch['speed_mm_min'], ch['gas']) == (900, 'nitrogen')
    assert d.cut_thickness == 8
    db.session.add(MachineCutTechnology(machine_id=laser.id, grade_id=None, thickness_mm=8, gas='nitrogen',
                                        nozzle='1.5', cutting_speed_mm_per_min=2500, peak_power_w=3000))
    db.session.commit()
    ch = fscut.parse(_write(tmp_path / 'b.lxd', c.get(url).data))['technology']['channels'][0]
    assert (ch['speed_mm_min'], ch['fields']['peak_power_w']) == (2500, 3000)   # the machine's row for this key
    m = fscut.parse(_write(tmp_path / 'c.lxd', c.get(url).data))['model']
    assert m['size'] == (100, 50) and m['cut_length_mm'] == 300 and m['pierces'] == 1

    # a new-format part file of the detail -> old .lxd; a nest file is not converted (step 1)
    part, part_path = _attach(d, 'Лицежа.lxds', _lxds(part=True))
    nest, nest_path = _attach(d, 'разкрой.lxds', _lxds())
    try:
        assert 'Изтегли като стар FSCUT' in c.get(f'/details/files/{part.id}/nest').get_data(as_text=True)
        r = c.get(f'/details/files/{part.id}/nest/lxd')
        m = fscut.parse(_write(tmp_path / 'p.lxd', r.data))['model']
        assert r.status_code == 200 and m['size'] == (300, 50)
        assert c.get(f'/details/files/{nest.id}/nest/lxd').status_code == 302
        assert _login('fn_worker').get(url).status_code == 302             # admin only
    finally:
        os.remove(part_path)
        os.remove(nest_path)
