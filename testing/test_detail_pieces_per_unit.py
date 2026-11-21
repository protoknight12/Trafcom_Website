"""
pytest: "Детайли от една единица" (Detail.pieces_per_unit, owner's decision 2026-11-21) - N details from one sheet /
bar / piece: one detail takes 1/N of that unit instead of its bounding box, in the stored material price (with the
standard markup), the price in another allowed material, and production consumption (_detail_material_unit_qty()).
Also the inline drawing on the detail page.

Run with:
    pytest testing/test_detail_pieces_per_unit.py -v
"""
import json

import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, MaterialPrice, MaterialType, Detail, DetailMaterial, Order, OrderItem,
                 limiter, seed_material_types, _detail_total_price, _detail_material_unit_qty)

SQUARE = [{'type': 'line', 'x1': 0, 'y1': 0, 'x2': 100, 'y2': 0}, {'type': 'line', 'x1': 100, 'y1': 0, 'x2': 100, 'y2': 100},
          {'type': 'line', 'x1': 100, 'y1': 100, 'x2': 0, 'y2': 100}, {'type': 'line', 'x1': 0, 'y1': 100, 'x2': 0, 'y2': 0}]


@pytest.fixture
def s():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        g.pop('company_info', None)
        g.pop('_material_types', None)
        db.create_all()
        seed_material_types()
        db.session.add(MaterialType(key='bolts', label='Болтове', price_unit='pcs', has_cutting=False, is_round=False,
                                    sort_order=9))
        db.session.add_all([
            # 50 €/m², sheet 1000 x 2000 = 2 m², 10 sheets on hand
            MaterialPrice(key='sheet', display_name='Лист', type='sheets', cost_per_m2=50, sheet_width_mm=1000,
                          sheet_length_mm=2000, stock_quantity=10),
            MaterialPrice(key='sheet2', display_name='Лист 2', type='sheets', cost_per_m2=100, sheet_width_mm=1000,
                          sheet_length_mm=1000, stock_quantity=5),
            MaterialPrice(key='nosize', display_name='Лист без размер', type='sheets', cost_per_m2=50),
            MaterialPrice(key='bar', display_name='Прът', type='rods', cost_per_m2=10, sheet_width_mm=20,
                          sheet_length_mm=6000, stock_quantity=60),
            MaterialPrice(key='bolt', display_name='Болт', type='bolts', cost_per_m2=3, stock_quantity=100),
        ])
        db.session.flush()
        common = dict(total_length=400, pierce_count=1, calculated_price=0)
        sheet_d = Detail(name='Пластина', material_key='sheet', width=100, height=100, geometry_json=json.dumps(SQUARE),
                         **common)
        bar_d = Detail(name='Ос', material_key='bar', width=20, height=500, **common)
        bolt_d = Detail(name='Втулка', material_key='bolt', width=10, height=10, **common)
        nosize_d = Detail(name='Без размер', material_key='nosize', width=100, height=100, **common)
        db.session.add_all([sheet_d, bar_d, bolt_d, nosize_d])
        db.session.flush()
        db.session.add(DetailMaterial(detail_id=sheet_d.id, material_key='sheet2'))
        pw = generate_password_hash('irrelevant123')
        adm = User(username='adm', password=pw, role='admin')
        db.session.add_all([adm, User(username='wrk', password=pw, role='worker')])
        db.session.flush()
        order = Order(order_number='ORD-P-1', user_id=adm.id, customer_name='QA', status='new')
        db.session.add(order)
        db.session.flush()
        item = OrderItem(order_id=order.id, detail_id=sheet_d.id, quantity_ordered=5, unit_price=5.0)
        db.session.add(item)
        db.session.commit()
        yield dict(sheet=sheet_d.id, bar=bar_d.id, bolt=bolt_d.id, nosize=nosize_d.id, item=item.id)
        db.session.remove()
        db.drop_all()
        g.pop('company_info', None)
        g.pop('_material_types', None)


def _login(name):
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': name, 'password': 'irrelevant123'})
    return c


def _set(c, detail_id, value=None, clear=False):
    data = {'clear': '1'} if clear else {'pieces_per_unit': str(value)}
    return c.post(f'/admin/details/{detail_id}/pieces-per-unit', data=data)


def _detail(detail_id):
    db.session.expire_all()
    return db.session.get(Detail, detail_id)


def test_sheet_one_tenth(s):
    c = _login('adm')
    assert _set(c, s['sheet'], 10).status_code == 302
    d = _detail(s['sheet'])
    assert d.pieces_per_unit == 10
    assert d.calculated_price == 12.0            # 2 m² / 10 = 0.2 m² x 50 €/m² x 1.2 (standard markup), bbox ignored
    assert (d.width, d.height) == (100, 100)     # its real size stays
    assert '1/10 от лист 1000×2000' in d.material_price_breakdown
    assert _set(c, s['sheet'], clear=True).status_code == 302
    d = _detail(s['sheet'])
    assert d.pieces_per_unit is None and d.calculated_price == 0.6   # back to 0.01 m² x 50 x 1.2


def test_linear_and_per_piece(s):
    c = _login('adm')
    _set(c, s['bar'], 4)
    d = _detail(s['bar'])
    assert _detail_material_unit_qty(d) == 1.5   # 6000 mm bar / 4
    assert d.calculated_price == 18.0            # 1.5 m x 10 €/m x 1.2
    _set(c, s['bolt'], 4)
    d = _detail(s['bolt'])
    assert _detail_material_unit_qty(d) == 0.25
    assert d.calculated_price == 0.9             # 3 € / 4 x 1.2


def test_sheet_without_size_keeps_bbox(s):
    c = _login('adm')
    _set(c, s['nosize'], 10)
    d = _detail(s['nosize'])
    assert d.pieces_per_unit == 10
    assert _detail_material_unit_qty(d) == pytest.approx(0.01)
    assert d.calculated_price == 0.6


def test_alternative_material_uses_its_own_unit(s):
    c = _login('adm')
    _set(c, s['sheet'], 10)
    d = _detail(s['sheet'])
    alt = MaterialPrice.query.filter_by(key='sheet2').one()
    # 1 m² / 10 = 0.1 m² x 100 €/m² x 1.2 = 12, no operations
    assert _detail_total_price(d, alt) == 12.0
    assert _detail_material_unit_qty(d, alt) == pytest.approx(0.1)


def test_production_takes_one_nth_sheet_and_order_price_frozen(s):
    c = _login('adm')
    _set(c, s['sheet'], 10)
    r = c.post('/admin/production', data={'target_type': 'item', 'target_id': s['item'], 'produced_qty': '1'})
    assert r.get_json()['status'] == 'success'
    db.session.expire_all()
    lot = MaterialPrice.query.filter_by(key='sheet').one()
    assert lot.stock_quantity == pytest.approx(9.9)          # 1/10 of one sheet left the lot
    assert db.session.get(OrderItem, s['item']).unit_price == 5.0


@pytest.mark.parametrize('bad', ['0', '-1', '2.5', 'abc', '10001', ''])
def test_validation(s, bad):
    c = _login('adm')
    assert _set(c, s['sheet'], bad).status_code == 302
    assert _detail(s['sheet']).pieces_per_unit is None


def test_worker_refused(s):
    c = _login('wrk')
    _set(c, s['sheet'], 10)
    assert _detail(s['sheet']).pieces_per_unit is None


def test_page_shows_viewer_and_cut_length(s):
    c = _login('adm')
    html = c.get(f'/details/{s["sheet"]}/files').get_data(as_text=True)
    assert 'id="detail_dxf_canvas"' in html and 'Дължина на рязане' in html and '400 мм (0.4 м)' in html
    assert 'id="pieces_per_unit"' in html
    assert c.get(f'/admin/details/{s["sheet"]}/geometry').get_json()['shapes'] == SQUARE
    html = c.get(f'/details/{s["bar"]}/files').get_data(as_text=True)   # no drawing -> no viewer
    assert 'id="detail_dxf_canvas"' not in html
