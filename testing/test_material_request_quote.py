"""
pytest: a supplier's quote on a material request (status 'quoted' "Оферирана", owner's request 2026-11-23) prices a
lot with no stock - material_cost_rate() in app.py: cost_per_m2, except our lot at stock 0 with a quote (latest
quote wins). Stored Detail prices follow when the quote changes or the lot runs out / gets stock
(_reprice_quoted_lots()); frozen order prices never change.

Run with:
    pytest testing/test_material_request_quote.py -v
"""
import json

import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, Supplier, MaterialPrice, MaterialRequest, Detail, Order,
                 OrderItem, limiter, seed_material_types, material_cost_rate, material_quote,
                 material_stock_unit_price, _detail_material_price)

# 500 x 400 mm = 0.2 m² = 0.1 of a 1000 x 2000 sheet. Delivery 10 €/m² -> 0.2 x 10 x 1.2 = 2.40;
# a quote of 30 € per sheet = 15 €/m² -> 0.2 x 15 x 1.2 = 3.60.
DELIVERY_PRICE, QUOTE_PRICE = 2.4, 3.6


@pytest.fixture
def s():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        g.pop('company_info', None)
        db.create_all()
        seed_material_types()
        pw = generate_password_hash('irrelevant123')
        customer_client = Client(name='Клиент Б')
        db.session.add(customer_client)
        db.session.flush()
        db.session.add_all([User(username='adm', password=pw, role='admin'),
                            User(username='wrk', password=pw, role='worker'),
                            User(username='cust', password=pw, role='regular_user', client_id=customer_client.id)])
        supplier = Supplier(name='Метал Оферта ООД')
        steel = MaterialPrice(key='steel', display_name='Стомана 2мм', cost_per_m2=10, type='sheets',
                              sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=2, stock_quantity=0)
        stocked = MaterialPrice(key='alu', display_name='Алуминий 2мм', cost_per_m2=10, type='sheets',
                                sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=2, stock_quantity=5)
        client_lot = MaterialPrice(key='steel_c', display_name='Стомана клиент', cost_per_m2=0, type='sheets',
                                   sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=0,
                                   owner_client_id=customer_client.id)
        db.session.add_all([supplier, steel, stocked, client_lot])
        db.session.flush()
        detail = Detail(name='Планка', material_key='steel', width=500, height=400, total_length=1, pierce_count=1,
                        calculated_price=DELIVERY_PRICE, stock_quantity=0)
        alu_detail = Detail(name='Планка алу', material_key='alu', width=500, height=400, total_length=1,
                            pierce_count=1, calculated_price=DELIVERY_PRICE, stock_quantity=0)
        db.session.add_all([detail, alu_detail])
        db.session.flush()
        req = MaterialRequest(material_id=steel.id, quantity=3)
        alu_req = MaterialRequest(material_id=stocked.id, quantity=3)
        db.session.add_all([req, alu_req])
        order = Order(order_number='ORD-Q-1', user_id=1, customer_name='QA', status='new')
        db.session.add(order)
        db.session.flush()
        item = OrderItem(order_id=order.id, detail_id=detail.id, quantity_ordered=10, unit_price=DELIVERY_PRICE)
        db.session.add(item)
        db.session.commit()
        yield dict(supplier=supplier.id, steel=steel.id, stocked=stocked.id, client_lot=client_lot.id,
                   detail=detail.id, alu_detail=alu_detail.id, req=req.id, alu_req=alu_req.id, item=item.id,
                   client=customer_client.id)
        db.session.remove()
        db.drop_all()
        g.pop('company_info', None)


def _login(name):
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': name, 'password': 'irrelevant123'})
    return c


def _quote(c, req_id, status='quoted', supplier='', price=''):
    return c.post(f'/storage/requests/{req_id}/update',
                  data={'status': status, 'supplier_id': str(supplier), 'quoted_unit_price': str(price)})


def _price(detail_id):
    db.session.expire_all()
    return db.session.get(Detail, detail_id).calculated_price


def _req(req_id):
    db.session.expire_all()
    return db.session.get(MaterialRequest, req_id)


def test_quote_needs_supplier_and_positive_price(s):
    wrk = _login('wrk')
    _quote(wrk, s['req'], price=30)                                  # no supplier
    _quote(wrk, s['req'], supplier=s['supplier'], price=0)
    _quote(wrk, s['req'], supplier=s['supplier'], price='abc')
    _quote(wrk, s['req'], supplier=s['supplier'])                    # no price
    r = _req(s['req'])
    assert r.status == 'new' and r.quoted_unit_price is None and r.supplier_id is None
    assert _price(s['detail']) == DELIVERY_PRICE
    _quote(wrk, s['req'], supplier=s['supplier'], price=30)           # a worker may quote
    r = _req(s['req'])
    assert r.status == 'quoted' and r.quoted_unit_price == 30 and r.supplier_id == s['supplier'] and r.quoted_at
    page = wrk.get('/storage/requests').get_data(as_text=True)
    assert 'Оферирана' in page and 'лист 1000×2000' in page and '15.00 €/м²' in page


def test_quoted_lot_without_stock_prices_details_from_the_quote(s):
    adm = _login('adm')
    _quote(adm, s['req'], supplier=s['supplier'], price=30)
    assert _price(s['detail']) == QUOTE_PRICE                         # stored price repriced, 20 % markup
    steel = db.session.get(MaterialPrice, s['steel'])
    assert material_cost_rate(steel) == 15 and steel.cost_per_m2 == 10   # the delivery price itself is untouched
    page = adm.get(f'/details/{s["detail"]}/files').get_data(as_text=True)
    assert 'по оферта от Метал Оферта ООД' in page
    assert 'оферта: 30.00 € / лист 1000×2000' in adm.get('/storage/materials').get_data(as_text=True)
    assert 'оферта: 30.00 €' in adm.get('/admin/materials').get_data(as_text=True)
    # the frozen order price never changes
    assert db.session.get(OrderItem, s['item']).unit_price == DELIVERY_PRICE
    # offer "Материал" line: one sheet at the quote + 20 %
    assert material_stock_unit_price(db.session.get(MaterialPrice, s['steel'])) == 36.0
    # 'ordered' keeps the quote
    _quote(adm, s['req'], status='ordered', supplier=s['supplier'], price=30)
    assert _req(s['req']).quoted_unit_price == 30 and _price(s['detail']) == QUOTE_PRICE


def test_stocked_lot_ignores_the_quote(s):
    adm = _login('adm')
    _quote(adm, s['alu_req'], supplier=s['supplier'], price=30)
    assert _price(s['alu_detail']) == DELIVERY_PRICE
    alu = db.session.get(MaterialPrice, s['stocked'])
    assert material_quote(alu) is None and material_cost_rate(alu) == 10
    assert material_stock_unit_price(alu) == 24.0


def test_delivery_brings_back_the_delivery_price_and_running_out_the_quote(s):
    adm = _login('adm')
    _quote(adm, s['req'], supplier=s['supplier'], price=30)
    assert _price(s['detail']) == QUOTE_PRICE
    line = {'type': 'material', 'name': 'Стомана 2мм', 'material_key': 'steel', 'qty': 1, 'unit_price': 20,
            'width': 1000, 'height': 2000, 'thickness': 2, 'material_type': 'sheets'}
    adm.post('/admin/delivery-notes/create', data={'items_json': json.dumps([line]), 'note_number': 'D1'})
    db.session.expire_all()
    assert db.session.get(MaterialPrice, s['steel']).stock_quantity == 1
    assert _price(s['detail']) == DELIVERY_PRICE                    # 20 € per sheet = the same 10 €/m² lot
    # production for the order takes 10 x 0.1 = the whole sheet -> stock 0 -> the quote again
    res = adm.post('/admin/production', data={'target_type': 'item', 'target_id': s['item'], 'produced_qty': '10'})
    assert res.get_json()['status'] == 'success'
    db.session.expire_all()
    assert abs(db.session.get(MaterialPrice, s['steel']).stock_quantity) < 1e-9
    assert _price(s['detail']) == QUOTE_PRICE
    assert db.session.get(OrderItem, s['item']).unit_price == DELIVERY_PRICE


def test_clearing_or_deleting_the_quote_reprices_back(s):
    adm = _login('adm')
    _quote(adm, s['req'], supplier=s['supplier'], price=30)
    _quote(adm, s['req'], status='processing')
    r = _req(s['req'])
    assert r.quoted_unit_price is None and r.quoted_at is None and r.supplier_id is None
    assert _price(s['detail']) == DELIVERY_PRICE
    _quote(adm, s['req'], supplier=s['supplier'], price=30)
    assert _price(s['detail']) == QUOTE_PRICE
    adm.post(f'/storage/requests/{s["req"]}/delete')
    assert _req(s['req']) is None and _price(s['detail']) == DELIVERY_PRICE


def test_latest_quote_wins(s):
    adm = _login('adm')
    _quote(adm, s['req'], supplier=s['supplier'], price=30)
    adm.post('/storage/requests/create', data={'items_json': json.dumps([{'material_id': s['steel'], 'quantity': 2}])})
    newer = MaterialRequest.query.order_by(MaterialRequest.id.desc()).first().id
    _quote(adm, newer, supplier=s['supplier'], price=40)              # 40 / 2 m² = 20 €/m² -> 0.2 x 20 x 1.2
    assert _price(s['detail']) == 4.8
    _quote(adm, s['req'], supplier=s['supplier'], price=36)           # re-quoting the older one makes it the latest
    assert _price(s['detail']) == round(0.2 * 18 * 1.2, 2)


def test_client_lot_unaffected(s):
    db.session.add(MaterialRequest(material_id=s['client_lot'], quantity=1, status='quoted', supplier_id=s['supplier'],
                                   quoted_unit_price=50))
    db.session.commit()
    lot = db.session.get(MaterialPrice, s['client_lot'])
    assert material_quote(lot) is None and material_cost_rate(lot) == 0


def test_pieces_per_unit_uses_the_quoted_rate(s):
    adm = _login('adm')
    _quote(adm, s['req'], supplier=s['supplier'], price=30)
    adm.post(f'/admin/details/{s["detail"]}/pieces-per-unit', data={'pieces_per_unit': '4'})
    assert _price(s['detail']) == round(0.5 * 15 * 1.2, 2)           # 1/4 of a 2 m² sheet at 15 €/m²
    assert _detail_material_price(db.session.get(Detail, s['detail'])) == 9.0


def test_customer_sees_no_quote(s):
    adm = _login('adm')
    _quote(adm, s['req'], supplier=s['supplier'], price=30)
    cust = _login('cust')
    for url in ('/orders/new', f'/details/{s["detail"]}/files'):
        page = cust.get(url).get_data(as_text=True)
        assert 'Метал Оферта' not in page and 'оферта' not in page.lower().replace('оферти', '')
