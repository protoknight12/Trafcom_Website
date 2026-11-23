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
import math
import os
import struct
import zipfile

import pytest
from flask import g
from werkzeug.security import generate_password_hash

import fscut
from app import app as flask_app, db, User, MaterialPrice, Detail, DetailDxfFile, limiter, seed_material_types

TECH = ('<Technical MaterialName="碳钢" Thickness="8"><Channels><Channel ChannelPort="1">'
        '<CamParams WorkSpeed="50"><Cut GasType="2" GasPressure=".7" PeakPower="6000" Focus="11" Height=".6"/>'
        '<Pierce1 TimeFollow="500" TimeStay="100"/></CamParams></Channel>'
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


def _lxds():
    """New format: plate polyline + a placed part made of a closed polyline (100x50, bulge 0) and a circle r=5 +
    a 50 mm line and a half arc, all in Geometry2D/data.bin."""
    poly = struct.pack('<dII', 0, 1, 4) + b''.join(
        struct.pack('<I3dI', i + 1, x, y, 0, 0) for i, (x, y) in enumerate([(10, 10), (110, 10), (110, 60), (10, 60)]))
    plate = struct.pack('<dII', 0, 1, 4) + b''.join(
        struct.pack('<I3dI', i + 1, x, y, 0, 0) for i, (x, y) in enumerate([(0, 0), (1000, 0), (1000, 2000), (0, 2000)]))
    records = [('LwPolyline', poly), ('Circle', _pt(60, 35) + struct.pack('<dIdd', 5, 0, 0, 0)),
               ('Line', _pt(200, 10) + _pt(250, 10)),
               ('Arc', _pt(300, 10) + struct.pack('<dIdd', 10, 0, 0, math.pi)), ('LwPolyline', plate)]
    geo, addrs = bytearray(b'BCMP' + b'\x00' * 4092), []
    for cls, payload in records:
        addrs.append(len(geo))
        geo += _record(cls, payload)
    shapes = ''.join(f'<{cls} Handle="{100 + i}" LayerName="0" GeoAddr="{a}"/>'
                     for i, ((cls, _), a) in enumerate(zip(records, addrs)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('info.xml', '<PackData><Application AppName="CypCutE" AppVer="6.4.2330.6"/></PackData>')
        z.writestr('Technical/content.xml', TECH)
        z.writestr('Shapes2D/content.xml', f'<Shapes2D>{shapes}</Shapes2D>')
        z.writestr('Geometry2D/data.bin', bytes(geo))
        z.writestr('Blocks/content.xml', '<Blocks><Block Name="{G1}"><BasePoint/><Shapes>'
                   + ''.join(f'<Shape Handle="{100 + i}"/>' for i in range(4)) + '</Shapes></Block></Blocks>')
        z.writestr('Nest2D/Parts/content.xml', '<Parts><NestPart Handle="7" Name="Планка" Amount="3" AmountUsed="1"/></Parts>')
        z.writestr('Nest2D/Plates/content.xml', '<Plates><NestPlate Handle="9" AmountUsed="1"><LwPolyline><Point/>'
                   '<Point X="1000"/><Point X="1000" Y="2000"/><Point Y="2000"/></LwPolyline></NestPlate></Plates>')
        z.writestr('Nest2D/Results/content.xml', '<NestResults><NestResult Index="1" Name="Nest result" Utilization="5"/></NestResults>')
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
    assert fscut.bbox(r['shapes'])[:2] == (10, 0)                        # the half arc reaches y = 0
    arc = [s for s in r['shapes'] if s['type'] == 'arc'][0]
    assert (round(arc['start_angle']), round(arc['end_angle'])) == (0, 180)

    bad = tmp_path / 'c.lxds'
    bad.write_bytes(b'not a nesting file')
    with pytest.raises(ValueError):
        fscut.parse(str(bad))
    bad.write_bytes(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><LXDDocument>&a;</LXDDocument>')
    with pytest.raises(ValueError):                                      # no DTD / entities accepted
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

        w = _login('fn_worker')
        assert w.get(f'/details/files/{f.id}/nest').status_code == 302          # admin only
    finally:
        for p in (path, other_path, broken_path):
            os.remove(p)
