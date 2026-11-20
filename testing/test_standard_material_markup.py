"""
pytest: the standard material markup (owner's decision 2026-11-19, /admin/company "Стандартна надценка за материали",
default 20%) - _material_terms() in app.py: a client with own Материали terms (any type, any percent incl. 0) gets
those, everyone else (no client too) delivery cost x (1 + markup). Stored Detail prices follow it (data step,
/admin/company change); frozen order prices never do.

Run with:
    pytest testing/test_standard_material_markup.py -v
"""
import io
import runpy

import ezdxf
import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, Order, OrderItem, Service, EditableText,
                 limiter, calculate_material_price, calculate_cnc_price_multi_service, detail_price_for,
                 _detail_material_price, BASE_SETUP_FEE)


def _square_dxf():
    doc = ezdxf.new()
    msp = doc.modelspace()
    pts = [(0, 0), (1000, 0), (1000, 1000), (0, 1000), (0, 0)]
    for a, b in zip(pts, pts[1:]):
        msp.add_line(a, b)
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode('utf-8')


@pytest.fixture
def s():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        g.pop('company_info', None)
        db.create_all()
        db.session.add(MaterialPrice(key='steel', display_name='Стомана', type='sheets', cost_per_m2=100,
                                     cutting_speed_mm_per_min=1000, pierce_rate_per_min=60))
        markup = Client(name='Надценка', material_adjustment_type='markup', material_adjustment_percent=10)
        discount = Client(name='Отстъпка', material_adjustment_type='discount', material_adjustment_percent=10)
        zero = Client(name='Нула', material_adjustment_type='markup', material_adjustment_percent=0)
        plain = Client(name='Без условия')
        db.session.add_all([markup, discount, zero, plain])
        db.session.flush()
        own_lot = MaterialPrice(key='own_steel', display_name='Стомана на клиента', type='sheets', cost_per_m2=100,
                                owner_client_id=plain.id)
        service = Service(name='Лазер', price_per_hour_eur=60)
        db.session.add_all([own_lot, service])
        pw = generate_password_hash('irrelevant123')
        db.session.add(User(username='adm', password=pw, role='admin'))
        db.session.commit()
        yield dict(markup=markup, discount=discount, zero=zero, plain=plain, service=service)
        db.session.remove()
        db.drop_all()
        g.pop('company_info', None)


def _admin():
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': 'adm', 'password': 'irrelevant123'})
    return c


def _set_markup(value):
    row = db.session.get(EditableText, 'settings.company.material_markup_percent')
    if row is None:
        db.session.add(EditableText(key='settings.company.material_markup_percent', content=str(value)))
    else:
        row.content = str(value)
    db.session.commit()
    g.pop('company_info', None)


def test_material_price_rule(s):
    assert calculate_material_price(1000, 1000, 'steel') == 120               # no client: 20 % by default
    assert calculate_material_price(1000, 1000, 'steel', s['plain']) == 120   # no own terms: 20 %
    assert calculate_material_price(1000, 1000, 'steel', s['markup']) == 110  # own terms replace it
    assert calculate_material_price(1000, 1000, 'steel', s['discount']) == 90
    assert calculate_material_price(1000, 1000, 'steel', s['zero']) == 100    # own 0 % = plain delivery price
    assert calculate_material_price(1000, 1000, 'own_steel', s['plain']) == 0  # client's own lot: nothing, no markup
    _set_markup(0)
    assert calculate_material_price(1000, 1000, 'steel') == 100


def test_dxf_calculator_no_client_gets_standard_markup(s):
    sid = [s['service'].id]
    # 1 m² of steel; 4000 mm at 1000 mm/min = 4 min + 1 pierce at 60/min -> (4 + 1/60) min x 1 €/min, + setup fee
    t = 4 + 1 / 60
    assert calculate_cnc_price_multi_service(1000, 1000, 4000, 1, 'steel', sid) == round(120 + t + BASE_SETUP_FEE, 2)
    assert calculate_cnc_price_multi_service(1000, 1000, 4000, 1, 'steel', sid, s['plain']) == round(120 + t + BASE_SETUP_FEE, 2)
    assert calculate_cnc_price_multi_service(1000, 1000, 4000, 1, 'steel', sid, s['discount']) == round(90 + t + BASE_SETUP_FEE, 2)
    # client's own material: no material cost at all
    assert calculate_cnc_price_multi_service(1000, 1000, 4000, 1, 'steel', sid, s['plain'], own_material=True) == round(t + BASE_SETUP_FEE, 2)


def test_public_and_owned_detail_prices(s):
    adm = _admin()
    for name, owner, expected in (('Публичен', '', 120), ('На надценка', s['markup'].id, 110),
                                  ('На нула', s['zero'].id, 100), ('На без условия', s['plain'].id, 120)):
        r = adm.post('/admin/details/add', data={'name': name, 'material': 'steel', 'owner_client_id': str(owner),
                                                 'file': (io.BytesIO(_square_dxf()), 'sq.dxf')},
                     content_type='multipart/form-data')
        assert r.status_code == 302
        d = Detail.query.filter_by(name=name).one()
        assert d.calculated_price == expected
        assert _detail_material_price(d) == expected                # stored == recomputed
    public = Detail.query.filter_by(name='Публичен').one()
    assert detail_price_for(public, s['markup']) == 120             # a public detail's material is not the client's DXF terms
    owned = Detail.query.filter_by(name='На надценка').one()
    assert detail_price_for(owned, s['markup']) == 110              # owner: stored price, no Детайли on top


def test_client_own_lot_detail_unaffected(s):
    d = Detail(name='От неговия лист', material_key='own_steel', width=1000, height=1000, total_length=40,
               pierce_count=1, calculated_price=0, owner_client_id=s['plain'].id)
    db.session.add(d)
    db.session.commit()
    assert d.total_price == 0
    assert calculate_material_price(1000, 1000, 'own_steel', s['plain']) == 0


def test_changing_markup_reprices_details_not_orders(s):
    d = Detail(name='Пластина', material_key='steel', width=1000, height=1000, total_length=40, pierce_count=1,
               calculated_price=120)
    manual = Detail(name='Ръчна цена', material_key='steel', width=0, height=0, total_length=0, pierce_count=0,
                    calculated_price=33)
    db.session.add_all([d, manual])
    db.session.flush()
    order = Order(order_number='ORD-T-1', customer_name='X', user_id=User.query.one().id)
    db.session.add(order)
    db.session.flush()
    db.session.add(OrderItem(order_id=order.id, detail_id=d.id, quantity_ordered=1, unit_price=120))
    db.session.commit()
    adm = _admin()
    r = adm.post('/admin/company', data={'name': 'ТРАФКОМ ООД', 'vat_percent': '20', 'material_markup_percent': '25'})
    assert r.status_code == 302
    db.session.expire_all()
    g.pop('company_info', None)
    assert db.session.get(Detail, d.id).calculated_price == 125
    assert db.session.get(Detail, manual.id).calculated_price == 33   # manual price kept
    assert OrderItem.query.one().unit_price == 120                      # frozen order price unchanged
    # invalid value refused, nothing changes
    adm.post('/admin/company', data={'name': 'ТРАФКОМ ООД', 'vat_percent': '20', 'material_markup_percent': 'abc'})
    db.session.expire_all()
    g.pop('company_info', None)
    assert db.session.get(Detail, d.id).calculated_price == 125


def test_client_terms_change_reprices_owned_details(s):
    d = Detail(name='Негова', material_key='steel', width=1000, height=1000, total_length=40, pierce_count=1,
               calculated_price=120, owner_client_id=s['plain'].id)
    db.session.add(d)
    db.session.commit()
    adm = _admin()
    adm.post(f'/admin/clients/{s["plain"].id}/pricing/update',
             data={'material_adjustment_type': 'markup', 'material_adjustment_percent': '0'})
    db.session.expire_all()
    c = db.session.get(Client, s['plain'].id)
    assert (c.material_adjustment_type, c.material_adjustment_percent) == ('markup', 0)   # 0 % own terms are kept
    assert db.session.get(Detail, d.id).calculated_price == 100


def test_data_step_idempotent(s):
    d = Detail(name='Стара', material_key='steel', width=1000, height=1000, total_length=40, pierce_count=1,
               calculated_price=100)   # the plain delivery price from before the rule
    db.session.add(d)
    db.session.commit()
    for _ in range(2):
        runpy.run_module('migration.data_apply_standard_material_markup', run_name='__main__')
        db.session.expire_all()
        assert db.session.get(Detail, d.id).calculated_price == 120


def test_client_delivery_note_material_prices(s):
    """The client delivery-note page gets each of OUR lots' sale price per stock unit (sheet without a size: m²):
    the standard markup by default, a client's own terms for him; a client's own lot gets none."""
    import json, re
    html = _admin().get('/admin/client-delivery-notes').get_data(as_text=True)
    prices = json.loads(re.search(r'const MATERIAL_PRICES = (\{.*?\});\n', html).group(1))
    steel = MaterialPrice.query.filter_by(key='steel').one()
    own = MaterialPrice.query.filter_by(key='own_steel').one()
    p = prices[str(steel.id)]
    assert p[''] == 120                                   # no client / client without terms: +20 %
    assert p[str(s['markup'].id)] == 110 and p[str(s['discount'].id)] == 90 and p[str(s['zero'].id)] == 100
    assert str(s['plain'].id) not in p                    # falls back to ''
    assert str(own.id) not in prices
