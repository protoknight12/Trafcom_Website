"""
pytest: cutting plan per sheet ("разкрой по листове", OrderCutSheet). admin_cut_plan_save() stores
how many pieces of each order line are cut from each sheet of a sheet lot (validated: whole counts,
never more than a line still needs, cut sheets untouched, no plan after piece-by-piece consumption);
the reservation then counts whole planned sheets plus the unplanned pieces; admin_cut_sheet() takes
exactly one whole sheet, produces the sheet's pieces (Detail stock up, no fractional consumption on
top), records the sheet's 1.0 on the lines and asks the remnant question for THAT sheet
(admin_order_remnants() with cut_sheet_id). The production report's delta input is refused on a
planned lot, a client's lot may not go below zero, the remnant prompt carries used / left per sheet
(plan and no-plan groups), and deleting the order gives the cut sheets back.

Run with:
    pytest testing/test_order_cut_plan.py -v
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

from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, Product, Order, OrderItem,
                 OrderItemComponent, OrderMaterialReservation, OrderMaterialRemnant, OrderCutSheet, limiter,
                 seed_material_types, num3)


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
                            sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=0.5,
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
        box = Product(name='Кутия', markup_percent=0)
        db.session.add_all([a, b, private, box])
        db.session.flush()
        orders = {}
        for number, lines in (('ORD-P', [(a, 10), (b, 5)]), ('ORD-C', [(private, 1)]), ('ORD-K', [(box, 3)])):
            order = Order(order_number=number, user_id=admin.id, customer_name='QA Клиент', status='new')
            db.session.add(order)
            db.session.flush()
            items = [OrderItem(order_id=order.id, product_id=d.id if d is box else None,
                               detail_id=None if d is box else d.id, quantity_ordered=q, unit_price=1.0)
                     for d, q in lines]
            db.session.add_all(items)
            db.session.flush()
            orders[number] = (order.id, [i.id for i in items])
        component = OrderItemComponent(order_item_id=orders['ORD-K'][1][0], detail_id=a.id,
                                       detail_name_snapshot=a.name, quantity_needed=6)
        db.session.add(component)
        db.session.commit()
        yield dict(ours=ours.id, lot=lot.id, a=a.id, b=b.id, private=private.id, component=component.id,
                   orders=orders)
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
        return [(s.id, s.seq, s.status, {f'{i.target_type}:{i.target_id}': i.quantity for i in s.items})
                for s in OrderCutSheet.query.filter_by(order_id=order_id).order_by(OrderCutSheet.seq)]


def _cut(c, sheet_id):
    return c.post(f'/admin/production/cut-sheets/{sheet_id}/cut')


def _decide(c, order_id, material_id, sheets, cut_sheet_id=None):
    data = {'order_id': order_id, 'material_id': material_id, 'sheets': json.dumps(sheets)}
    if cut_sheet_id:
        data['cut_sheet_id'] = cut_sheet_id
    return c.post('/admin/production/remnants', data=data)


def _get(model, row_id):
    with flask_app.app_context():
        row = db.session.get(model, row_id)
        db.session.expunge(row)
        return row


def test_plan_validation(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    a, b = f'item:{a_item}', f'item:{b_item}'
    for bad in ([{a: 6}, {a: 5}],               # 11 of 10
                [{a: 1.5}], [{a: -1}], [{a: True}],
                [{'item:999999': 1}]):           # not a line of this lot
        res = _plan(c, order_id, ids['ours'], bad)
        assert res.status_code == 400 and res.get_json()['status'] == 'error', bad
    assert _sheets(order_id) == []
    res = _plan(c, order_id, ids['ours'], [{a: 6, b: 2}, {}, {a: 4, b: 3}])   # the empty sheet is dropped
    assert res.get_json()['status'] == 'success', res.get_json()
    assert [(seq, status, items) for _, seq, status, items in _sheets(order_id)] == [
        (1, 'planned', {a: 6, b: 2}), (2, 'planned', {a: 4, b: 3})]
    # the client's lot is a different group: these lines are not on it
    assert _plan(c, order_id, ids['lot'], [{a: 1}]).status_code == 400


def test_reservation_counts_planned_sheets_and_blocks_the_cut(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    c.post(f'/admin/orders/{order_id}/confirm-material')
    with flask_app.app_context():
        assert OrderMaterialReservation.query.filter_by(order_id=order_id).one().quantity == pytest.approx(1.5)
    _plan(c, order_id, ids['ours'], [{f'item:{a_item}': 6, f'item:{b_item}': 2}])
    with flask_app.app_context():   # 1 whole sheet + (4 + 3) unplanned pieces x 0.1
        assert OrderMaterialReservation.query.filter_by(order_id=order_id).one().quantity == pytest.approx(1.7)
        db.session.get(MaterialPrice, ids['ours']).stock_quantity = 1.0
        db.session.commit()
    [(sheet_id, _, _, _)] = _sheets(order_id)
    res = _cut(c, sheet_id)
    assert res.status_code == 400 and 'не стига' in res.get_json()['message']
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(1.0)
    assert _get(OrderItem, a_item).quantity_produced == 0


def test_cut_takes_one_whole_sheet_and_produces_its_pieces(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    a, b = f'item:{a_item}', f'item:{b_item}'
    _plan(c, order_id, ids['ours'], [{a: 6, b: 2}])
    [(sheet_id, _, _, _)] = _sheets(order_id)
    data = _cut(c, sheet_id).get_json()
    assert data['status'] == 'success', data
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(9)      # exactly one sheet, nothing fractional
    item_a, item_b = _get(OrderItem, a_item), _get(OrderItem, b_item)
    assert (item_a.quantity_produced, item_b.quantity_produced) == (6, 2)
    assert (_get(Detail, ids['a']).stock_quantity, _get(Detail, ids['b']).stock_quantity) == (6, 2)
    # the sheet's 1.0 split by area: 1.2 m² and 0.4 m²
    assert (item_a.material_consumed, item_b.material_consumed) == (pytest.approx(0.75), pytest.approx(0.25))
    assert item_a.consumed_material_id == item_b.consumed_material_id == ids['ours']
    prompt = data['remnant_prompt']
    assert (prompt['cut_sheet_id'], prompt['seq'], prompt['sheets'], prompt['estimate']) == (sheet_id, 1, 1, True)
    assert prompt['usage'] == [{'used_m2': 1.6, 'used_pct': 80.0, 'left_m2': 0.4, 'left_pct': 20.0}]

    again = _cut(c, sheet_id)                                          # a cut sheet is final
    assert again.status_code == 400 and _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(9)
    # re-planning keeps the cut sheet, numbers new ones after it, and only allows what is left (4 A, 3 B)
    assert _plan(c, order_id, ids['ours'], [{a: 5}]).status_code == 400
    assert _plan(c, order_id, ids['ours'], [{a: 4, b: 3}]).get_json()['status'] == 'success'
    assert [(seq, status, items) for _, seq, status, items in _sheets(order_id)] == [
        (1, 'cut', {a: 6, b: 2}), (2, 'planned', {a: 4, b: 3})]
    # the delta input of the report is refused for a planned lot
    res = c.post('/admin/production', data={'target_type': 'item', 'target_id': a_item, 'produced_qty': '7'})
    assert res.status_code == 400 and 'разкрой' in res.get_json()['message']
    assert _get(OrderItem, a_item).quantity_produced == 6


def test_remnant_is_decided_per_sheet(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    a, b = f'item:{a_item}', f'item:{b_item}'
    _plan(c, order_id, ids['ours'], [{a: 6, b: 2}, {a: 4, b: 3}])
    (first, *_), (second, *_) = _sheets(order_id)
    assert _cut(c, first).get_json()['remnant_prompt']['cut_sheet_id'] == first
    res = _decide(c, order_id, ids['ours'], [{'action': 'scrap'}], cut_sheet_id=first)
    assert res.get_json()['status'] == 'success', res.get_json()
    assert _decide(c, order_id, ids['ours'], [{'action': 'scrap'}], cut_sheet_id=first).status_code == 400
    prompt = _cut(c, second).get_json()['remnant_prompt']
    assert prompt['usage'][0]['used_m2'] == pytest.approx(1.4)
    assert _decide(c, order_id, ids['ours'], [{'action': 'scrap'}]).status_code == 400    # no group-level question
    res = _decide(c, order_id, ids['ours'], [{'action': 'return', 'width': 600, 'height': 400}], cut_sheet_id=second)
    assert res.get_json()['status'] == 'success', res.get_json()
    with flask_app.app_context():
        assert db.session.get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(8)
        [rest] = MaterialPrice.query.filter(MaterialPrice.display_name == 'Стомана 2мм - остатък').all()
        assert (rest.sheet_width_mm, rest.sheet_length_mm, rest.stock_quantity) == (600, 400, 1)
        decisions = OrderMaterialRemnant.query.order_by(OrderMaterialRemnant.id).all()
        assert [(r.cut_sheet_id, r.sheets, r.scrap_count, r.extra_taken) for r in decisions] == [
            (first, 1, 1, 0.0), (second, 1, 0, 0.0)]
        assert db.session.get(Order, order_id).status == 'completed'
    page = c.get('/admin/production').get_data(as_text=True)
    assert 'class="remnant-pending"' not in page
    # history: the lines' shares of the two sheets add up to the 2 sheets taken (10 -> 8)
    item_a, item_b = _get(OrderItem, a_item), _get(OrderItem, b_item)
    assert item_a.material_consumed + item_b.material_consumed == pytest.approx(2)
    history = c.get(f'/admin/materials/{ids["ours"]}/history').get_data(as_text=True)
    # screens show quantities to the 3rd decimal (|num3)
    assert f'-{num3(item_a.material_consumed)}<' in history and f'-{num3(item_b.material_consumed)}<' in history


def test_component_line_on_a_sheet(c, ids):
    order_id, (product_item,) = ids['orders']['ORD-K']
    key = f'component:{ids["component"]}'
    assert _plan(c, order_id, ids['ours'], [{key: 6}]).get_json()['status'] == 'success'
    [(sheet_id, _, _, _)] = _sheets(order_id)
    data = _cut(c, sheet_id).get_json()
    assert data['status'] == 'success', data
    component = _get(OrderItemComponent, ids['component'])
    assert (component.quantity_produced, component.material_consumed) == (6, pytest.approx(1.0))
    assert _get(Detail, ids['a']).stock_quantity == 6
    assert data['remnant_prompt']['usage'][0]['used_pct'] == 60.0


def test_client_lot_may_not_go_negative(c, ids):
    order_id, (item,) = ids['orders']['ORD-C']
    assert _plan(c, order_id, ids['lot'], [{f'item:{item}': 1}]).get_json()['status'] == 'success'
    [(sheet_id, _, _, _)] = _sheets(order_id)
    res = _cut(c, sheet_id)                                         # 0.5 sheet on hand, a whole one needed
    assert res.status_code == 400 and 'клиентска партида' in res.get_json()['message']
    assert _get(MaterialPrice, ids['lot']).stock_quantity == pytest.approx(0.5)
    assert _get(OrderItem, item).quantity_produced == 0 and _sheets(order_id)[0][2] == 'planned'


def test_no_plan_group_keeps_the_old_way_with_usage(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    c.post('/admin/production', data={'target_type': 'item', 'target_id': a_item, 'produced_qty': '2'})
    # material consumed piece by piece before any plan -> no plan for this group
    assert _plan(c, order_id, ids['ours'], [{f'item:{b_item}': 1}]).status_code == 400
    c.post('/admin/production', data={'target_type': 'item', 'target_id': a_item, 'produced_qty': '10'})
    prompt = c.post('/admin/production', data={'target_type': 'item', 'target_id': b_item,
                                               'produced_qty': '5'}).get_json()['remnant_prompt']
    assert (prompt['sheets'], prompt['cut_sheet_id'], prompt['estimate']) == (2, None, False)
    assert prompt['usage'] == [{'used_m2': 2.0, 'used_pct': 100.0, 'left_m2': 0.0, 'left_pct': 0.0},
                               {'used_m2': 1.0, 'used_pct': 50.0, 'left_m2': 1.0, 'left_pct': 50.0}]
    res = _decide(c, order_id, ids['ours'], [{'action': 'scrap'}, {'action': 'scrap'}])
    assert res.get_json()['status'] == 'success'
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(8)
    assert _plan(c, order_id, ids['ours'], []).status_code == 400            # remnant decided


def test_order_delete_gives_the_cut_sheets_back(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    _plan(c, order_id, ids['ours'], [{f'item:{a_item}': 6, f'item:{b_item}': 2}, {f'item:{a_item}': 1}])
    first = _sheets(order_id)[0][0]
    _cut(c, first)
    _decide(c, order_id, ids['ours'], [{'action': 'return', 'width': 600, 'height': 400}], cut_sheet_id=first)
    with flask_app.app_context():
        rest_id = MaterialPrice.query.filter_by(display_name='Стомана 2мм - остатък').one().id
    c.post(f'/admin/orders/{order_id}/delete')
    with flask_app.app_context():
        order = db.session.get(Order, order_id)
        assert order.deleted_at is not None
        assert db.session.get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(10)
        assert db.session.get(MaterialPrice, rest_id).stock_quantity == pytest.approx(0)
        assert db.session.get(Detail, ids['a']).stock_quantity == 0 and db.session.get(Detail, ids['b']).stock_quantity == 0


def test_untouched_planned_order_is_hard_deleted(c, ids):
    order_id, (a_item, _) = ids['orders']['ORD-P']
    _plan(c, order_id, ids['ours'], [{f'item:{a_item}': 6}])
    c.post(f'/admin/orders/{order_id}/delete')
    with flask_app.app_context():
        assert db.session.get(Order, order_id) is None and OrderCutSheet.query.count() == 0


def test_order_edit_and_line_removal_with_a_plan(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    _plan(c, order_id, ids['ours'], [{f'item:{a_item}': 6}])
    page = c.post(f'/admin/order-items/{a_item}/delete', follow_redirects=True).get_data(as_text=True)
    assert 'разкрой' in page and _get(OrderItem, a_item) is not None
    _cut(c, _sheets(order_id)[0][0])
    page = c.post(f'/admin/orders/{order_id}/edit', data={'customer_name': 'QA', f'qty_{a_item}': '5'},
                  follow_redirects=True).get_data(as_text=True)
    assert 'не може да е под 6' in page and _get(OrderItem, a_item).quantity_ordered == 10
    page = c.post(f'/admin/orders/{order_id}/edit', data={'customer_name': 'QA', f'qty_{a_item}': '8'},
                  follow_redirects=True).get_data(as_text=True)
    assert 'редактирайте разкроя' in page and _get(OrderItem, a_item).quantity_ordered == 8


def test_report_shows_the_plan_button_and_sheets(c, ids):
    order_id, (a_item, b_item) = ids['orders']['ORD-P']
    page = c.get('/admin/production').get_data(as_text=True)
    assert 'Разкрой по листове (' in page and 'data-plan=' in page
    _plan(c, order_id, ids['ours'], [{f'item:{a_item}': 6, f'item:{b_item}': 2}])
    page = c.get('/admin/production').get_data(as_text=True)
    assert 'Лист 1: Планка А x6, Планка Б x2' in page and 'cutSheet(' in page
