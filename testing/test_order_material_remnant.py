"""
pytest: once an order's lines on a sheet lot (price unit 'm2') are all produced, the production report
asks what became of the rest of every sheet used (_pending_remnants(), `remnant_prompt` in the
report's JSON) and admin_order_remnants() records it: the lot gives up whole sheets (ceil of what the
lines consumed, at least 1), a returned sheet becomes a "<name> - остатък" lot of the entered size (same
owner and €/m²), scrap stays written off, OrderMaterialRemnant makes the decision final. Lines sharing a
lot are one group, a client's lot may not go below zero, other material types never ask, a remnant
larger than the sheet is refused, an undecided group shows a badge on the report and a lot referenced by
a decision cannot be deleted.

Run with:
    pytest testing/test_order_material_remnant.py -v
"""
import atexit
import json
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)


def _cleanup_db_file():
    try:
        os.remove(_db_path)
    except OSError:
        pass  # Windows keeps the file locked as long as SQLAlchemy's pooled connection is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, Order, OrderItem,
                 OrderMaterialRemnant, limiter, seed_material_types)


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        owner = Client(name='Клиент А')
        db.session.add_all([admin, owner])
        db.session.flush()
        speeds = dict(cutting_speed_mm_per_min=1000, pierce_rate_per_min=10)
        ours = MaterialPrice(key='steel', display_name='Стомана 2мм', cost_per_m2=10, type='sheets', thickness_mm=2,
                             sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=10, **speeds)
        lot = MaterialPrice(key='steel_c', display_name='Стомана клиент', cost_per_m2=0, type='sheets',
                            sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=1.0,
                            owner_client_id=owner.id, **speeds)
        rods = MaterialPrice(key='rod', display_name='Прът', cost_per_m2=3, type='rods', sheet_length_mm=6000,
                             stock_quantity=60)
        db.session.add_all([ours, lot, rods])
        db.session.flush()
        # 500 x 400 mm = 0.2 m² per piece = 0.1 of a 2 m² sheet; 1000 x 1200 = 0.6 of a sheet
        a = Detail(name='Планка А', material_key='steel', width=500, height=400, total_length=1, pierce_count=1,
                   calculated_price=5.0, stock_quantity=0)
        b = Detail(name='Планка Б', material_key='steel', width=500, height=400, total_length=1, pierce_count=1,
                   calculated_price=5.0, stock_quantity=0)
        private = Detail(name='Планка клиент', material_key='steel_c', width=1000, height=1200, total_length=1,
                         pierce_count=1, calculated_price=0.0, stock_quantity=0, owner_client_id=owner.id)
        bar = Detail(name='Ос', material_key='rod', width=20, height=500, total_length=1, pierce_count=0,
                     calculated_price=1.0, stock_quantity=0)
        db.session.add_all([a, b, private, bar])
        db.session.flush()
        orders = {}
        for number, lines in (('ORD-R-2', [(a, 10), (b, 5)]), ('ORD-R-1', [(a, 6)]),
                              ('ORD-R-C', [(private, 1)]), ('ORD-R-ROD', [(bar, 4)])):
            order = Order(order_number=number, user_id=admin.id, customer_name='QA Клиент', status='new')
            db.session.add(order)
            db.session.flush()
            items = [OrderItem(order_id=order.id, detail_id=d.id, quantity_ordered=q, unit_price=1.0) for d, q in lines]
            db.session.add_all(items)
            db.session.flush()
            orders[number] = (order.id, [i.id for i in items])
        db.session.commit()
        yield dict(ours=ours.id, lot=lot.id, rods=rods.id, orders=orders)
        db.session.remove()
        db.drop_all()


@pytest.fixture
def c(ids):
    client = flask_app.test_client()
    client.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return client


def _produce(c, item_id, qty):
    res = c.post('/admin/production', data={'target_type': 'item', 'target_id': item_id, 'produced_qty': str(qty)})
    data = res.get_json()
    assert data['status'] == 'success', data
    return data['remnant_prompt']


def _decide(c, order_id, material_id, sheets):
    return c.post('/admin/production/remnants', data={'order_id': order_id, 'material_id': material_id,
                                                      'sheets': json.dumps(sheets)})


def _stock(material_id):
    with flask_app.app_context():
        return db.session.get(MaterialPrice, material_id).stock_quantity


def _remnant_lots():
    with flask_app.app_context():
        return [(m.id, m.display_name, m.sheet_width_mm, m.sheet_length_mm, m.stock_quantity, m.cost_per_m2,
                 m.owner_client_id, m.cutting_speed_mm_per_min)
                for m in MaterialPrice.query.filter(MaterialPrice.display_name.like('% - остатък'))]


def test_two_lines_on_one_lot_are_one_group_ready_after_the_last(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-R-2']
    assert _produce(c, a_item, 10) is None          # Планка Б (same lot) not produced yet
    prompt = _produce(c, b_item, 5)                 # 1.0 + 0.5 sheet consumed -> 2 sheets
    assert prompt['order_id'] == order_id and prompt['material_id'] == ids['ours']
    assert prompt['sheets'] == 2 and (prompt['default_w'], prompt['default_h']) == (1000, 2000)
    assert 'Стомана 2мм' in prompt['material_name']


def test_one_sheet_scrapped_takes_the_whole_sheet(c, ids):
    order_id, (item,) = ids['orders']['ORD-R-1']
    prompt = _produce(c, item, 3)
    assert prompt is None                           # 3 of 6
    prompt = _produce(c, item, 6)
    assert prompt['sheets'] == 1                    # 0.6 of a sheet, ceil -> 1
    assert _stock(ids['ours']) == pytest.approx(10 - 0.6)
    res = _decide(c, order_id, ids['ours'], [{'action': 'scrap'}])
    assert res.get_json()['status'] == 'success'
    assert _stock(ids['ours']) == pytest.approx(9)  # the extra 0.4 written off with the sheet
    assert _remnant_lots() == []
    with flask_app.app_context():
        r = OrderMaterialRemnant.query.one()
        assert (r.sheets, r.scrap_count, r.returned) == (1, 1, [])
        assert r.extra_taken == pytest.approx(0.4)

    again = _decide(c, order_id, ids['ours'], [{'action': 'scrap'}])   # deciding twice
    assert again.status_code == 400 and again.get_json()['status'] == 'error'
    assert _stock(ids['ours']) == pytest.approx(9)
    history = c.get(f'/admin/materials/{ids["ours"]}/history').get_data(as_text=True)
    assert 'Брак - остатък от лист' in history and '-0.4<' in history


def test_mixed_scrap_and_return_creates_a_remnant_lot(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-R-2']
    _produce(c, a_item, 10)
    _produce(c, b_item, 5)
    bad = _decide(c, order_id, ids['ours'], [{'action': 'scrap'}])     # 2 sheets used, one answer
    assert bad.status_code == 400
    res = _decide(c, order_id, ids['ours'], [{'action': 'scrap'}, {'action': 'return', 'width': 600, 'height': 400}])
    assert res.get_json()['status'] == 'success', res.get_json()
    assert _stock(ids['ours']) == pytest.approx(8)  # two whole sheets out
    [(lot_id, name, w, h, stock, cost, owner, speed)] = _remnant_lots()
    assert (name, w, h, stock, cost, owner, speed) == ('Стомана 2мм - остатък', 600, 400, 1, 10, None, 1000)
    history = c.get(f'/admin/materials/{ids["ours"]}/history').get_data(as_text=True)
    assert 'Брак - остатък от лист' in history and '-0.5<' in history
    remnant_history = c.get(f'/admin/materials/{lot_id}/history').get_data(as_text=True)
    assert 'Остатък от лист - върнат в склада' in remnant_history and '600x400 mm' in remnant_history
    assert 'ORD-R-2' in remnant_history and '+1<' in remnant_history

    # a lot a decision refers to cannot be deleted (no ON DELETE on the reference)
    with flask_app.app_context():
        key = db.session.get(MaterialPrice, lot_id).key
    c.post(f'/admin/materials/{key}/delete')
    with flask_app.app_context():
        assert db.session.get(MaterialPrice, lot_id) is not None


def test_client_lot_keeps_its_owner_and_cannot_go_negative(c, ids):
    order_id, (item,) = ids['orders']['ORD-R-C']
    prompt = _produce(c, item, 1)                   # 0.6 of the client's 1.0
    assert prompt['sheets'] == 1
    res = _decide(c, order_id, ids['lot'], [{'action': 'return', 'width': 300, 'height': 300}])
    assert res.get_json()['status'] == 'success'
    assert _stock(ids['lot']) == pytest.approx(0, abs=1e-9)
    [(_, name, w, h, stock, cost, owner, _)] = _remnant_lots()
    with flask_app.app_context():
        owner_id = db.session.get(MaterialPrice, ids['lot']).owner_client_id
    assert (name, w, h, stock, cost, owner) == ('Стомана клиент - остатък', 300, 300, 1, 0, owner_id)


def test_client_lot_true_up_below_zero_is_refused(c, ids):
    with flask_app.app_context():
        db.session.get(MaterialPrice, ids['lot']).stock_quantity = 0.7
        db.session.commit()
    order_id, (item,) = ids['orders']['ORD-R-C']
    _produce(c, item, 1)                            # 0.7 - 0.6 = 0.1 left, a whole sheet needs 0.4 more
    res = _decide(c, order_id, ids['lot'], [{'action': 'scrap'}])
    assert res.status_code == 400 and 'клиентска партида' in res.get_json()['message']
    assert _stock(ids['lot']) == pytest.approx(0.1)
    with flask_app.app_context():
        assert OrderMaterialRemnant.query.count() == 0


def test_non_sheet_material_never_asks(c, ids):
    order_id, (item,) = ids['orders']['ORD-R-ROD']
    assert _produce(c, item, 4) is None
    assert _decide(c, order_id, ids['rods'], [{'action': 'scrap'}]).status_code == 400
    assert _stock(ids['rods']) == pytest.approx(60 - 2)


def test_remnant_larger_than_the_sheet_is_refused(c, ids):
    order_id, (item,) = ids['orders']['ORD-R-1']
    _produce(c, item, 6)
    for w, h in ((1100, 1100), (100, 2100), (0, 300)):
        res = _decide(c, order_id, ids['ours'], [{'action': 'return', 'width': w, 'height': h}])
        assert res.status_code == 400, (w, h)
    assert _remnant_lots() == [] and _stock(ids['ours']) == pytest.approx(10 - 0.6)
    res = _decide(c, order_id, ids['ours'], [{'action': 'return', 'width': 1900, 'height': 900}])  # turned
    assert res.get_json()['status'] == 'success'


def test_later_leaves_a_badge_until_decided(c, ids):
    order_id, (item,) = ids['orders']['ORD-R-1']
    _produce(c, item, 6)                            # the dialog is closed with "По-късно": no POST
    page = c.get('/admin/production').get_data(as_text=True)
    assert page.count('class="remnant-pending"') == 1 and 'data-remnant=' in page
    _decide(c, order_id, ids['ours'], [{'action': 'scrap'}])
    assert 'class="remnant-pending"' not in c.get('/admin/production').get_data(as_text=True)
