"""
pytest: undoing a cut sheet ("Върни листа", admin_uncut_sheet()) and batches of identical sheets
(OrderCutSheet.copies). Undo is the exact reverse of admin_cut_sheet(): the whole sheets back in the
lot, the pieces out of produced and Detail stock, material_consumed back, the sheet's remnant decision
undone (scrap; or the returned sheets out of an untouched remnant lot - refused when it was used),
the sheet planned again, a completed order reopened; a client's Detail may not go negative. A batch
row of K sheets is validated as counts x K, reserves K whole sheets, is cut whole or in part (the row
splits), is asked about its remnant once (scrap + returned = k, one remnant lot of one size) and is
undone as a whole. The material history adds up to the lot's stock after every step.

Run with:
    pytest testing/test_order_cut_batch.py -v
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

import app as app_module
from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, Order, OrderItem,
                 OrderMaterialReservation, OrderMaterialRemnant, OrderCutSheet, limiter, seed_material_types)

REST = 'Стомана 2мм - остатък'


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
                            sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=2,
                            owner_client_id=owner.id, **speeds)
        db.session.add_all([ours, lot])
        db.session.flush()
        # 500 x 400 mm = 0.2 m² per piece = 0.1 of a 2 m² sheet
        a = Detail(name='Планка А', material_key='steel', width=500, height=400, total_length=1, pierce_count=1,
                   calculated_price=5.0, stock_quantity=0)
        b = Detail(name='Планка Б', material_key='steel', width=500, height=400, total_length=1, pierce_count=1,
                   calculated_price=5.0, stock_quantity=0)
        private = Detail(name='Планка клиент', material_key='steel_c', width=1000, height=1200, total_length=1,
                         pierce_count=1, calculated_price=0.0, stock_quantity=0, owner_client_id=owner.id)
        db.session.add_all([a, b, private])
        db.session.flush()
        orders = {}
        for number, lines in (('ORD-P', [(a, 10), (b, 5)]), ('ORD-C', [(private, 1)])):
            order = Order(order_number=number, user_id=admin.id, customer_name='QA Клиент', status='new')
            db.session.add(order)
            db.session.flush()
            items = [OrderItem(order_id=order.id, detail_id=d.id, quantity_ordered=q, unit_price=1.0) for d, q in lines]
            db.session.add_all(items)
            db.session.flush()
            orders[number] = (order.id, [i.id for i in items])
        db.session.commit()
        yield dict(ours=ours.id, lot=lot.id, a=a.id, b=b.id, private=private.id, orders=orders)
        db.session.remove()
        db.drop_all()


@pytest.fixture
def c(ids):
    client = flask_app.test_client()
    client.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return client


def _plan(c, order_id, material_id, sheets):
    return c.post('/admin/production/cut-plan', data={'order_id': order_id, 'material_id': material_id,
                                                      'sheets': json.dumps(sheets)})


def _sheets(order_id):
    with flask_app.app_context():
        return [(s.id, s.seq, s.status, s.copies, {f'{i.target_type}:{i.target_id}': i.quantity for i in s.items})
                for s in OrderCutSheet.query.filter_by(order_id=order_id).order_by(OrderCutSheet.seq)]


def _cut(c, sheet_id, count=None):
    return c.post(f'/admin/production/cut-sheets/{sheet_id}/cut', data={} if count is None else {'count': count})


def _undo(c, sheet_id):
    return c.post(f'/admin/production/cut-sheets/{sheet_id}/undo')


def _decide(c, order_id, material_id, sheets, cut_sheet_id):
    return c.post('/admin/production/remnants', data={'order_id': order_id, 'material_id': material_id,
                                                      'sheets': json.dumps(sheets), 'cut_sheet_id': cut_sheet_id})


def _get(model, row_id):
    with flask_app.app_context():
        row = db.session.get(model, row_id)
        db.session.expunge(row)
        return row


def _rest():
    with flask_app.app_context():
        lots = MaterialPrice.query.filter_by(display_name=REST).all()
        return [(m.id, m.sheet_width_mm, m.sheet_length_mm, m.stock_quantity) for m in lots]


def _history_adds_up(c, material_id, start):
    """start + every 'in' - every 'out' of admin_material_history() == the lot's stock now."""
    captured = {}

    def fake_render(template, **kw):
        captured.update(kw)
        return ''

    original = app_module.render_template
    app_module.render_template = fake_render
    try:
        c.get(f'/admin/materials/{material_id}/history')
    finally:
        app_module.render_template = original
    net = sum(m['quantity'] if m['direction'] == 'in' else -m['quantity'] for m in captured['movements'])
    assert start + net == pytest.approx(_get(MaterialPrice, material_id).stock_quantity)


def test_undo_single_sheet_restores_everything(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    a, b = f'item:{a_item}', f'item:{b_item}'
    c.post(f'/admin/orders/{order_id}/confirm-material')
    _plan(c, order_id, ids['ours'], [{a: 6, b: 2}, {a: 4, b: 3}])
    first = _sheets(order_id)[0][0]
    assert _cut(c, first).get_json()['status'] == 'success'
    assert _decide(c, order_id, ids['ours'], [{'action': 'scrap'}], first).get_json()['status'] == 'success'
    _history_adds_up(c, ids['ours'], 10)
    with flask_app.app_context():
        reserved = OrderMaterialReservation.query.filter_by(order_id=order_id).one().quantity

    res = _undo(c, first).get_json()
    assert res['status'] == 'success', res
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(10)
    for item_id, detail_id in ((a_item, ids['a']), (b_item, ids['b'])):
        item = _get(OrderItem, item_id)
        assert (item.quantity_produced, item.material_consumed, item.consumed_material_id) == (0, 0.0, None)
        assert _get(Detail, detail_id).stock_quantity == 0
    with flask_app.app_context():
        assert OrderMaterialRemnant.query.count() == 0
        sheet = db.session.get(OrderCutSheet, first)
        assert (sheet.status, sheet.cut_at, sheet.cut_by_id) == ('planned', None, None)
        assert OrderMaterialReservation.query.filter_by(order_id=order_id).one().quantity == pytest.approx(reserved)
        assert db.session.get(Order, order_id).status == 'new'
    _history_adds_up(c, ids['ours'], 10)
    assert _undo(c, first).status_code == 400                       # not cut any more
    # cut again works like the first time; the report's delta input points to "Върни листа"
    assert _cut(c, first).get_json()['status'] == 'success'
    assert _get(OrderItem, a_item).quantity_produced == 6
    res = c.post('/admin/production', data={'target_type': 'item', 'target_id': a_item, 'produced_qty': '7'})
    assert res.status_code == 400 and 'Върни листа' in res.get_json()['message']


def test_undo_takes_a_returned_remnant_back_or_refuses_when_used(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    _plan(c, order_id, ids['ours'], [{f'item:{a_item}': 6, f'item:{b_item}': 2}])
    first = _sheets(order_id)[0][0]
    _cut(c, first)
    _decide(c, order_id, ids['ours'], [{'action': 'return', 'width': 600, 'height': 400}], first)
    [(rest_id, _, _, stock)] = _rest()
    assert stock == 1
    with flask_app.app_context():                                     # the remnant was used since
        db.session.get(MaterialPrice, rest_id).stock_quantity = 0
        db.session.commit()
    res = _undo(c, first)
    assert res.status_code == 400 and REST in res.get_json()['message']
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(9)
    assert _sheets(order_id)[0][2] == 'cut' and _get(OrderItem, a_item).quantity_produced == 6
    with flask_app.app_context():
        assert OrderMaterialRemnant.query.count() == 1
        db.session.get(MaterialPrice, rest_id).stock_quantity = 1
        db.session.commit()
    assert _undo(c, first).get_json()['status'] == 'success'
    assert _rest() == [(rest_id, 600, 400, 0)]                         # left at 0, not removed
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(10)
    _history_adds_up(c, ids['ours'], 10)


def test_undo_reopens_a_completed_order(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    _plan(c, order_id, ids['ours'], [{f'item:{a_item}': 10, f'item:{b_item}': 5}])
    first = _sheets(order_id)[0][0]
    _cut(c, first)
    assert _get(Order, order_id).status == 'completed'
    page = c.get('/admin/production').get_data(as_text=True)   # the dialog still opens for "Върни листа"
    assert 'data-plan=' in page and '&#34;editable&#34;: false' in page
    assert _undo(c, first).get_json()['status'] == 'success'
    assert _get(Order, order_id).status == 'new'


def test_undo_refused_when_a_client_detail_would_go_negative(c, ids):
    order_id, (item,) = ids['orders']['ORD-C']
    _plan(c, order_id, ids['lot'], [{f'item:{item}': 1}])
    first = _sheets(order_id)[0][0]
    assert _cut(c, first).get_json()['status'] == 'success'
    assert _get(MaterialPrice, ids['lot']).stock_quantity == pytest.approx(1)
    with flask_app.app_context():                                     # the part went to the client meanwhile
        db.session.get(Detail, ids['private']).stock_quantity = 0
        db.session.commit()
    res = _undo(c, first)
    assert res.status_code == 400 and 'клиентска партида' in res.get_json()['message']
    assert _get(OrderItem, item).quantity_produced == 1 and _sheets(order_id)[0][2] == 'cut'
    assert _get(MaterialPrice, ids['lot']).stock_quantity == pytest.approx(1)


def test_batch_plan_validation_and_reservation(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    a, b = f'item:{a_item}', f'item:{b_item}'
    c.post(f'/admin/orders/{order_id}/confirm-material')
    for bad in ([{a: 3, 'copies': 4}],                            # 12 of 10
                [{a: 2, b: 2, 'copies': 3}],                      # 6 of 5 B
                [{a: 1, 'copies': 0}], [{a: 1, 'copies': -1}], [{a: 1, 'copies': 1.5}], [{a: 1, 'copies': True}]):
        res = _plan(c, order_id, ids['ours'], bad)
        assert res.status_code == 400 and res.get_json()['status'] == 'error', bad
    assert _plan(c, order_id, ids['ours'], [{a: 2, b: 1, 'copies': 4}, {a: 1}]).get_json()['status'] == 'success'
    assert [(seq, status, copies, items) for _, seq, status, copies, items in _sheets(order_id)] == [
        (1, 'planned', 4, {a: 2, b: 1}), (2, 'planned', 1, {a: 1})]
    with flask_app.app_context():   # 5 whole sheets + (1 A + 1 B) unplanned x 0.1
        assert OrderMaterialReservation.query.filter_by(order_id=order_id).one().quantity == pytest.approx(5.2)
    page = c.get('/admin/production').get_data(as_text=True)
    assert 'Лист 1 (×4 листа): Планка А x2, Планка Б x1' in page and 'id="cut-count-' in page


def test_batch_cut_partial_then_rest(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    a, b = f'item:{a_item}', f'item:{b_item}'
    _plan(c, order_id, ids['ours'], [{a: 2, b: 1, 'copies': 4}])
    [(row, *_)] = _sheets(order_id)
    for bad in (0, 5, 'x'):
        assert _cut(c, row, bad).status_code == 400
    data = _cut(c, row, 3).get_json()
    assert data['status'] == 'success', data
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(7)
    item_a, item_b = _get(OrderItem, a_item), _get(OrderItem, b_item)
    assert (item_a.quantity_produced, item_b.quantity_produced) == (6, 3)
    assert (_get(Detail, ids['a']).stock_quantity, _get(Detail, ids['b']).stock_quantity) == (6, 3)
    assert (item_a.material_consumed, item_b.material_consumed) == (pytest.approx(2), pytest.approx(1))
    rows = _sheets(order_id)
    assert [(seq, status, copies, items) for _, seq, status, copies, items in rows] == [
        (1, 'cut', 3, {a: 2, b: 1}), (2, 'planned', 1, {a: 2, b: 1})]
    prompt = data['remnant_prompt']
    assert (prompt['cut_sheet_id'], prompt['sheets'], prompt['consumed']) == (row, 3, 3.0)
    assert prompt['usage'] == [{'used_m2': 0.6, 'used_pct': 30.0, 'left_m2': 1.4, 'left_pct': 70.0}]
    assert _cut(c, rows[1][0]).get_json()['status'] == 'success'      # default = all its copies (1)
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(6)
    assert _get(OrderItem, a_item).quantity_produced == 8
    _history_adds_up(c, ids['ours'], 10)


def test_batch_remnant_is_one_question_and_one_lot(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    a, b = f'item:{a_item}', f'item:{b_item}'
    _plan(c, order_id, ids['ours'], [{a: 2, b: 1, 'copies': 4}])
    [(row, *_)] = _sheets(order_id)
    _cut(c, row, 3)
    res = _decide(c, order_id, ids['ours'], [{'action': 'scrap'}, {'action': 'scrap'}], row)
    assert res.status_code == 400 and 'точно 3' in res.get_json()['message']
    ret = {'action': 'return', 'width': 600, 'height': 400}
    res = _decide(c, order_id, ids['ours'], [{'action': 'scrap'}, ret, ret], row)
    assert res.get_json()['status'] == 'success', res.get_json()
    [(rest_id, w, h, stock)] = _rest()
    assert (w, h, stock) == (600, 400, 2)
    with flask_app.app_context():
        decision = OrderMaterialRemnant.query.filter_by(cut_sheet_id=row).one()
        assert (decision.sheets, decision.scrap_count, decision.extra_taken) == (3, 1, 0.0)
        assert decision.returned == [{'width': 600, 'height': 400, 'material_id': rest_id}] * 2
    # the rest of the batch: a same-size remnant goes into the same lot
    rest_row = _sheets(order_id)[1][0]
    _cut(c, rest_row)
    assert _decide(c, order_id, ids['ours'], [ret], rest_row).get_json()['status'] == 'success'
    assert _rest() == [(rest_id, 600, 400, 3)]
    _history_adds_up(c, ids['ours'], 10)
    _history_adds_up(c, rest_id, 0)


def test_batch_undo_and_order_delete(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    a, b = f'item:{a_item}', f'item:{b_item}'
    _plan(c, order_id, ids['ours'], [{a: 2, b: 1, 'copies': 4}])
    [(row, *_)] = _sheets(order_id)
    _cut(c, row, 3)
    ret = {'action': 'return', 'width': 600, 'height': 400}
    _decide(c, order_id, ids['ours'], [{'action': 'scrap'}, ret, ret], row)
    [(rest_id, *_)] = _rest()
    assert _undo(c, row).get_json()['status'] == 'success'           # the whole cut row, all 3 sheets
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(10)
    assert _get(MaterialPrice, rest_id).stock_quantity == pytest.approx(0)
    assert (_get(OrderItem, a_item).quantity_produced, _get(Detail, ids['a']).stock_quantity) == (0, 0)
    assert [(status, copies) for _, _, status, copies, _ in _sheets(order_id)] == [('planned', 3), ('planned', 1)]
    _history_adds_up(c, ids['ours'], 10)
    _history_adds_up(c, rest_id, 0)
    # cut again and delete the order: the batch and its remnants are reversed too
    _cut(c, row, 2)
    _decide(c, order_id, ids['ours'], [ret, ret], row)
    assert _get(MaterialPrice, rest_id).stock_quantity == pytest.approx(2)
    c.post(f'/admin/orders/{order_id}/delete')
    assert _get(Order, order_id).deleted_at is not None
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(10)
    assert _get(MaterialPrice, rest_id).stock_quantity == pytest.approx(0)
    assert _get(Detail, ids['a']).stock_quantity == 0
    _history_adds_up(c, ids['ours'], 10)
