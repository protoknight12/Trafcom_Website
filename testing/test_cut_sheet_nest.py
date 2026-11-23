"""
pytest: an FSCUT nest for one sheet row of an order's cutting plan (owner's request 2026-11-23).
admin_cut_plan_nest() keeps the uploaded file and gives the block its copies (the file's sheet count) and each nest part
with its count per sheet, matched to the order line by drawing (Detail.geometry_json) or by name; admin_cut_plan_save()
stores the file on the row (only a file of this order); the sheet's remnant (the sheet minus the parts grown by 10 mm)
downloads as DXF / old / new FSCUT plate; after "Изрязан" the remnant question offers the largest rectangle inside it,
and keeping it with its outline makes its own remnant lot with the outline (picture + plate files). The choice scrap /
stock stays the user's. A replaced plan's file is removed.

Run with:
    pytest testing/test_cut_sheet_nest.py -v
"""
import io
import json
import os

import ezdxf
import pytest
from werkzeug.security import generate_password_hash

import fscut
from app import (app as flask_app, db, User, MaterialPrice, Detail, Order, OrderItem, OrderCutSheet,
                 OrderMaterialRemnant, limiter, seed_material_types)


def _rect(h, x, y, w, hh, fill=True):
    pts = ''.join(f'<Point X="{px}" Y="{py}"/>' for px, py in ((x, y), (x + w, y), (x + w, y + hh), (x, y + hh)))
    return (f'<LwPolyline Handle="{h}" ChannelPort="1" LayerName="0"{" IsFill=\"true\"" if fill else ""} '
            f'PointCount="4" PolyFlag="1"><ToolCompensation/><MicroConnects/>{pts}</LwPolyline>')


def _nest(plate_amount=2):
    """Old-format nest on a 1000 x 2000 sheet: part "Детайл 1" (500 x 400, drawn like Планка А) twice and
    "планка б" (300 x 200) once, `plate_amount` sheets cut with it."""
    return ('<?xml version="1.0" encoding="utf-8"?><LXDDocument><Technical><Channels><Channel ChannelPort="1">'
            '<CamParams WorkSpeed="50"/></Channel></Channels></Technical>'
            '<Blocks><Block Name="P1"><BasePoint/><Entities>' + _rect(1, 0, 0, 500, 400) + '</Entities></Block>'
            '<Block Name="P2"><BasePoint/><Entities>' + _rect(2, 0, 0, 300, 200) + '</Entities></Block></Blocks>'
            '<NestParts><NestPart Name="Детайл 1" Amount="10"><Insert><BlockName>P1</BlockName></Insert></NestPart>'
            '<NestPart Name="планка б" Amount="5"><Insert><BlockName>P2</BlockName></Insert></NestPart></NestParts>'
            '<NestPlates><NestPlate Handle="9" Name="Rectangle" Amount="2" AmountUsed="2"><LwPolyline><Point/>'
            '<Point X="1000"/><Point X="1000" Y="2000"/><Point Y="2000"/></LwPolyline></NestPlate></NestPlates>'
            f'<NestResults><NestResult Name="R1" Utilization="25" PlateAmount="{plate_amount}"><Entities>'
            + _rect(10, 10, 10, 500, 400) + _rect(11, 10, 420, 500, 400) + _rect(12, 520, 10, 300, 200) +
            '<LwPolyline Handle="16" NestPlate="9" PointCount="4" PolyFlag="1"><Point/><Point X="1000"/>'
            '<Point X="1000" Y="2000"/><Point Y="2000"/></LwPolyline></Entities></NestResult></NestResults></LXDDocument>')


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        admin = User(username='cn_admin', password=generate_password_hash('irrelevant123'), role='admin')
        steel = MaterialPrice(key='cn_steel', display_name='Стомана 2мм', cost_per_m2=10, type='sheets', thickness_mm=2,
                              sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=10)
        db.session.add_all([admin, steel])
        db.session.flush()
        rect = [{'type': 'line', 'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2} for x1, y1, x2, y2 in
                ((0, 0, 500, 0), (500, 0, 500, 400), (500, 400, 0, 400), (0, 400, 0, 0))]
        a = Detail(name='Планка А', material_key='cn_steel', width=500, height=400, total_length=1800, pierce_count=1,
                   calculated_price=5.0, stock_quantity=0, geometry_json=json.dumps(rect))
        b = Detail(name='Планка Б', material_key='cn_steel', width=300, height=200, total_length=1000, pierce_count=1,
                   calculated_price=5.0, stock_quantity=0)
        db.session.add_all([a, b])
        db.session.flush()
        orders = {}
        for number in ('ORD-N1', 'ORD-N2'):
            order = Order(order_number=number, user_id=admin.id, customer_name='QA', status='new')
            db.session.add(order)
            db.session.flush()
            items = [OrderItem(order_id=order.id, detail_id=a.id, quantity_ordered=10, unit_price=1.0),
                     OrderItem(order_id=order.id, detail_id=b.id, quantity_ordered=5, unit_price=1.0)]
            db.session.add_all(items)
            db.session.flush()
            orders[number] = (order.id, [i.id for i in items])
        db.session.commit()
        yield dict(steel=steel.id, orders=orders)
        db.session.remove()
        db.drop_all()


@pytest.fixture
def c(ids):
    client = flask_app.test_client()
    client.post('/login', data={'username': 'cn_admin', 'password': 'irrelevant123'})
    return client


def _upload(c, order_id, material_id, data=None, name='лист 1.lxd'):
    return c.post('/admin/production/cut-plan/nest', data={
        'order_id': order_id, 'material_id': material_id,
        'file': (io.BytesIO((data or _nest()).encode() if isinstance(data or '', str) else data), name)},
        content_type='multipart/form-data')


def _path(name):
    return os.path.join(flask_app.config['ORDER_ITEM_FILE_FOLDER'], name)


def test_nest_fills_the_sheet_and_gives_the_remnant(c, ids, tmp_path):
    order_id, (ia, ib) = ids['orders']['ORD-N1']
    other_id, _ = ids['orders']['ORD-N2']
    r = _upload(c, order_id, ids['steel'])
    data = r.get_json()
    assert r.status_code == 200 and data['copies'] == 2 and data['name'] == 'лист 1.lxd'
    # "Детайл 1" is Планка А by its drawing, "планка б" Планка Б by name
    assert [(p['name'], p['count'], p['key']) for p in data['parts']] == \
           [('Детайл 1', 2, f'item:{ia}'), ('планка б', 1, f'item:{ib}')]
    token = data['token']
    assert os.path.isfile(_path(token)) and str(order_id) in token
    other_token = _upload(c, other_id, ids['steel']).get_json()['token']
    assert _upload(c, order_id, ids['steel'], name='x.dxf').status_code == 400          # not an FSCUT file
    assert _upload(c, order_id, ids['steel'], data=b'not a nest').status_code == 400

    plan = [{f'item:{ia}': 2, f'item:{ib}': 1, 'copies': 2, 'nest': token, 'nest_name': 'лист 1.lxd'},
            {f'item:{ia}': 1, 'copies': 1, 'nest': other_token}]                       # another order's file: dropped
    assert c.post('/admin/production/cut-plan', data={'order_id': order_id, 'material_id': ids['steel'],
                                                      'sheets': json.dumps(plan)}).status_code == 200
    with flask_app.app_context():
        rows = OrderCutSheet.query.filter_by(order_id=order_id).order_by(OrderCutSheet.seq).all()
        assert [(s.copies, s.nest_file, s.nest_file_name) for s in rows] == [(2, token, 'лист 1.lxd'), (1, None, None)]
        sheet_id = rows[0].id
    page = c.get('/admin/production').get_data(as_text=True)
    assert token in page and 'nest_name' in page                          # the plan dialog gets the row's file

    assert c.get(f'/admin/production/cut-sheets/{sheet_id}/nest').data == _nest().encode()
    r = c.get(f'/admin/production/cut-sheets/{sheet_id}/remnant/dxf?gap=10')
    assert r.status_code == 200
    (tmp_path / 'r.dxf').write_bytes(r.data)
    plines = list(ezdxf.readfile(str(tmp_path / 'r.dxf')).modelspace().query('LWPOLYLINE'))
    assert len(plines) == 1 and plines[0].closed
    for fmt in ('lxd', 'lxds'):
        r = c.get(f'/admin/production/cut-sheets/{sheet_id}/remnant/{fmt}')
        (tmp_path / f'r.{fmt}').write_bytes(r.data)
        plates = fscut.parse(str(tmp_path / f'r.{fmt}'))['plates']
        assert r.status_code == 200 and len(plates) == 1 and plates[0]['size'] == (1000, 2000)

    with flask_app.app_context():                                                       # the remnant itself
        rem = fscut.nest_remnant(fscut.parse(_path(token)), gap=10)
        # parts reach x 830 (520 + 300) + 10 mm gap: the free strip right of them is 1000 - 840 = 160 wide, the
        # area above y 830 (420 + 400 + 10) is 1000 x 1170 - the largest rectangle
        assert rem['rect'][0] >= 990 and 1160 <= rem['rect'][1] <= 1172
        assert 1.49 < rem['area_m2'] < 1.51          # 2 m² - (520 x 830 + 320 x 220 - their 10 x 220 overlap)

    # cut the batch: the remnant question offers the rectangle; keeping it with its outline = its own lot
    r = c.post(f'/admin/production/cut-sheets/{sheet_id}/cut')
    prompt = r.get_json()['remnant_prompt']
    assert prompt['suggest'] and prompt['suggest']['width'] >= 990 and len(prompt['suggest']['pts']) >= 4
    piece = {'width': prompt['suggest']['width'], 'height': prompt['suggest']['height'], 'count': 1}
    bad = [{'action': 'return', 'pieces': [piece, piece], 'outline': True}, {'action': 'scrap'}]
    assert c.post('/admin/production/remnants', data={'order_id': order_id, 'material_id': ids['steel'],
                                                      'cut_sheet_id': sheet_id, 'sheets': json.dumps(bad)}).status_code == 400
    good = [{'action': 'return', 'pieces': [piece], 'outline': True}, {'action': 'scrap'}]
    r = c.post('/admin/production/remnants', data={'order_id': order_id, 'material_id': ids['steel'],
                                                   'cut_sheet_id': sheet_id, 'sheets': json.dumps(good)})
    assert r.status_code == 200, r.get_json()
    with flask_app.app_context():
        decision = OrderMaterialRemnant.query.filter_by(cut_sheet_id=sheet_id).one()
        lot = db.session.get(MaterialPrice, decision.returned[0]['material_id'])
        assert lot.stock_quantity == 1 and 'ORD-N1 л.1' in lot.display_name
        assert lot.remnant_points and len(lot.remnant_points) == len(prompt['suggest']['pts'])
        assert (lot.sheet_width_mm, lot.sheet_length_mm) == (piece['width'], piece['height'])
        lot_id = lot.id
    page = c.get('/storage/materials').get_data(as_text=True)
    assert 'class="remnant-outline"' in page and f'/admin/materials/{lot_id}/remnant/lxds' in page
    r = c.get(f'/admin/materials/{lot_id}/remnant/lxd')
    (tmp_path / 'm.lxd').write_bytes(r.data)
    assert r.status_code == 200 and fscut.parse(str(tmp_path / 'm.lxd'))['plates'][0]['size'][0] == 1000
    assert c.get(f'/admin/materials/{ids["steel"]}/remnant/dxf').status_code == 404       # a lot without outline

    # a replaced plan drops the planned row's file (the cut row keeps its own)
    with flask_app.app_context():
        planned = OrderCutSheet.query.filter_by(order_id=order_id, status='planned').all()
    t2 = _upload(c, order_id, ids['steel']).get_json()['token']
    c.post('/admin/production/cut-plan', data={'order_id': order_id, 'material_id': ids['steel'],
                                               'sheets': json.dumps([{f'item:{ia}': 1, 'nest': t2}])})
    c.post('/admin/production/cut-plan', data={'order_id': order_id, 'material_id': ids['steel'],
                                               'sheets': json.dumps([{f'item:{ia}': 1}])})
    assert not os.path.exists(_path(t2)) and os.path.exists(_path(token))
    assert planned                                                                          # the plan had a row
    for name in os.listdir(flask_app.config['ORDER_ITEM_FILE_FOLDER']):
        if name.startswith('cutnest_'):
            os.remove(_path(name))


def test_nest_upload_refusals_and_worker(c, ids):
    order_id, _ = ids['orders']['ORD-N1']
    assert c.post('/admin/production/cut-plan/nest', data={'order_id': 'x'}).status_code == 400
    assert _upload(c, 999999, ids['steel']).status_code == 400
    assert c.get('/admin/production/cut-sheets/999999/remnant/dxf').status_code == 404
    from flask import g
    g.pop('_login_user', None)              # the logged-in user is cached on the shared app context
    anon = flask_app.test_client()
    assert anon.post('/admin/production/cut-plan/nest', data={}).status_code in (302, 401)
