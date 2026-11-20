"""
pytest: an admin edits and deletes placed orders. admin_delete_order() hard-deletes an order that never
moved stock (lines, reservations and PDFs gone, the offer freed) and soft-deletes one that did: produced
pieces leave the Detail stock, consumed material goes back to its frozen lot, a sheet remnant decision is
reversed (refused when its remnant lot was used), a client's Detail may not go negative, the order is
hidden everywhere (live_orders()) and the material history keeps the movement plus its reversal.
admin_order_edit() / admin_order_add_line() / admin_order_remove_line(): quantity never below the
produced, frozen unit_price, a new line priced exactly like create_order(), the reservation follows the
needs (a MaterialRequest only for a grown shortage, no rows stay no rows).

Run with:
    pytest testing/test_order_edit_delete.py -v
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
from flask import g
from flask_login import login_user
from werkzeug.security import generate_password_hash

import app as appmod
from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, Product, ProductDetail, Service,
                 Order, OrderItem, OrderItemComponent, OrderItemAttachment, OrderMaterialReservation,
                 OrderMaterialRemnant, MaterialRequest, Offer, limiter, seed_material_types)


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        pw = generate_password_hash('irrelevant123')
        admin = User(username='qa_admin', password=pw, role='admin')
        owner = Client(name='Клиент А', detail_adjustment_type='discount', detail_adjustment_percent=10)
        db.session.add_all([admin, owner, User(username='qa_worker', password=pw, role='worker')])
        db.session.flush()
        db.session.add(User(username='qa_customer', password=pw, role='regular_user', client_id=owner.id))
        speeds = dict(cutting_speed_mm_per_min=1000, pierce_rate_per_min=10)
        ours = MaterialPrice(key='steel', display_name='Стомана 2мм', cost_per_m2=10, type='sheets', thickness_mm=2,
                             sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=10, **speeds)
        lot = MaterialPrice(key='steel_c', display_name='Стомана клиент', cost_per_m2=0, type='sheets',
                            sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=1.0,
                            owner_client_id=owner.id, **speeds)
        service = Service(name='Огъване', price_per_hour_eur=60)
        db.session.add_all([ours, lot, service])
        db.session.flush()
        # 500 x 400 mm = 0.2 m² = 0.1 of a 2 m² sheet per piece
        detail = Detail(name='Планка', material_key='steel', width=500, height=400, total_length=1, pierce_count=1,
                        calculated_price=5.0, stock_quantity=0)
        private = Detail(name='Планка клиент', material_key='steel_c', width=500, height=400, total_length=1,
                         pierce_count=1, calculated_price=0.0, stock_quantity=0, owner_client_id=owner.id)
        product = Product(name='Изделие', markup_percent=0)
        db.session.add_all([detail, private, product])
        db.session.flush()
        db.session.add(ProductDetail(product_id=product.id, detail_id=detail.id, quantity=2))
        db.session.commit()
        yield dict(admin=admin.id, owner=owner.id, ours=ours.id, lot=lot.id, detail=detail.id, private=private.id,
                   product=product.id, service=service.id)
        db.session.remove()
        db.drop_all()


def _login(username):
    g.pop('_login_user', None)   # the fixture's app context would leak flask-login's cached user between clients
    client = flask_app.test_client()
    client.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return client


@pytest.fixture
def c(ids):
    return _login('qa_admin')


def _order(c, cart, client_id=''):
    g.pop('_login_user', None)
    before = {o.id for o in _all(Order)}
    res = c.post('/orders/new', data={'customer_name': 'QA', 'cart_json': json.dumps(cart), 'client_id': client_id})
    assert res.status_code == 302
    (new_id,) = {o.id for o in _all(Order)} - before
    return new_id


def _all(model, **filters):
    with flask_app.app_context():
        return model.query.filter_by(**filters).all()


def _get(model, row_id):
    with flask_app.app_context():
        return db.session.get(model, row_id)


def _items(order_id):
    return sorted(_all(OrderItem, order_id=order_id), key=lambda i: i.id)


def _produce(c, target_type, target_id, qty):
    g.pop('_login_user', None)
    return c.post('/admin/production', data={'target_type': target_type, 'target_id': target_id,
                                              'produced_qty': str(qty)}).get_json()


def _reserved(order_id):
    return {r.material_id: round(r.quantity, 6) for r in _all(OrderMaterialReservation, order_id=order_id)}


def _delete(c, order_id):
    g.pop('_login_user', None)
    return c.post(f'/admin/orders/{order_id}/delete', follow_redirects=True).get_data(as_text=True)


def _tool(tool, *args):
    with flask_app.test_request_context():
        login_user(User.query.filter_by(username='qa_admin').first())
        return tool.func(*args)


# ---------------------------------------------------------------- delete

def test_untouched_order_is_hard_deleted(c, ids):
    order_id = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 5},
                          {'type': 'product', 'id': ids['product'], 'qty': 1}])
    assert _reserved(order_id)
    folder = flask_app.config['ORDER_ITEM_FILE_FOLDER']
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, 'qa_order_delete_test.pdf')
    with open(path, 'wb') as f:
        f.write(b'%PDF-1.4')
    with flask_app.app_context():
        item = OrderItem.query.filter_by(order_id=order_id, detail_id=ids['detail']).one()
        db.session.add(OrderItemAttachment(order_item_id=item.id, filename='qa_order_delete_test.pdf',
                                           original_filename='x.pdf'))
        offer = Offer(number='00000000001', order_id=order_id)
        db.session.add(offer)
        db.session.commit()
        offer_id = offer.id
    page = _delete(c, order_id)
    assert 'е изтрита' in page
    assert _get(Order, order_id) is None
    assert not _all(OrderItem) and not _all(OrderItemComponent) and not _all(OrderItemAttachment)
    assert not _all(OrderMaterialReservation)
    assert _get(Offer, offer_id).order_id is None
    assert not os.path.exists(path)


def test_cancelled_order_is_listed_and_deleting_it_frees_the_product(c, ids):
    order_id = _order(c, [{'type': 'product', 'id': ids['product'], 'qty': 1}])
    with flask_app.app_context():
        db.session.get(Order, order_id).status = 'cancelled'
        db.session.commit()
    g.pop('_login_user', None)
    page = c.get('/admin/production').get_data(as_text=True)
    assert 'Отказани поръчки' in page and f'/admin/orders/{order_id}/delete' in page
    g.pop('_login_user', None)
    refused = c.post(f"/admin/products/{ids['product']}/delete", follow_redirects=True).get_data(as_text=True)
    assert 'не може да бъде изтрит' in refused
    assert 'е изтрита' in _delete(c, order_id)
    g.pop('_login_user', None)
    done = c.post(f"/admin/products/{ids['product']}/delete", follow_redirects=True).get_data(as_text=True)
    assert 'беше изтрит' in done


def test_produced_order_is_reversed_and_hidden(c, ids):
    order_id = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 5}])
    (item,) = _items(order_id)
    assert _produce(c, 'item', item.id, 3)['status'] == 'success'
    assert _get(Detail, ids['detail']).stock_quantity == 3
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(9.7)
    with flask_app.app_context():
        db.session.add(Offer(number='00000000002', order_id=order_id))
        db.session.commit()
    number = _get(Order, order_id).order_number

    page = _delete(c, order_id)
    assert 'стоковите разписки' in page or 'стокови разписки' in page
    assert _get(Detail, ids['detail']).stock_quantity == 0
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(10)
    order = _get(Order, order_id)
    assert order is not None and order.deleted_at is not None and order.deleted_by_id == ids['admin']
    assert _items(order_id)[0].material_consumed == pytest.approx(0.3)   # kept frozen for the history
    assert not _all(OrderMaterialReservation) and _all(Offer)[0].order_id is None

    g.pop('_login_user', None)
    for url in ('/admin/production', '/orders', '/admin/missing-stock'):
        assert number not in c.get(url).get_data(as_text=True), url
    assert number not in _tool(appmod.my_orders)
    assert 'Нямате поръчка' in _tool(appmod.order_status, number)
    assert 'Нямате поръчка' in _tool(appmod.order_missing_stock, number)
    history = c.get(f'/admin/materials/{ids["ours"]}/history').get_data(as_text=True)
    assert 'Производство по поръчка' in history and 'Изтрита поръчка - сторно' in history

    # nothing can move it any more, and a second delete is refused
    assert _produce(c, 'item', item.id, 5)['status'] == 'error'
    assert 'вече е изтрита' in _delete(c, order_id)
    assert _get(Detail, ids['detail']).stock_quantity == 0
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(10)
    assert c.get(f'/admin/orders/{order_id}/edit').status_code == 404


def _remnant_order(c, ids):
    """5 x Планка produced (0.5 of a sheet) and the rest decided: one sheet back as a 600x400 remnant."""
    order_id = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 5}])
    (item,) = _items(order_id)
    prompt = _produce(c, 'item', item.id, 5)['remnant_prompt']
    assert prompt['sheets'] == 1
    res = c.post('/admin/production/remnants', data={'order_id': order_id, 'material_id': ids['ours'],
                                                      'sheets': json.dumps([{'action': 'return', 'width': 600,
                                                                             'height': 400}])})
    assert res.get_json()['status'] == 'success'
    (remnant,) = _all(OrderMaterialRemnant)
    return order_id, item, remnant.returned[0]['material_id']


def test_remnant_decision_is_reversed(c, ids):
    order_id, item, remnant_lot = _remnant_order(c, ids)
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(9)
    assert _get(MaterialPrice, remnant_lot).stock_quantity == 1
    # the decided group's line cannot change any more
    page = c.post(f'/admin/orders/{order_id}/edit', data={'customer_name': 'QA', f'qty_{item.id}': '6'},
                  follow_redirects=True).get_data(as_text=True)
    assert 'вече е решен' in page and _items(order_id)[0].quantity_ordered == 5

    _delete(c, order_id)
    assert _get(Order, order_id).deleted_at is not None
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(10)
    assert _get(MaterialPrice, remnant_lot).stock_quantity == pytest.approx(0)
    assert _get(Detail, ids['detail']).stock_quantity == 0
    history = c.get(f'/admin/materials/{remnant_lot}/history').get_data(as_text=True)
    assert 'Остатък от лист - върнат в склада' in history and 'Изтрита поръчка - сторно' in history


def test_used_remnant_lot_refuses_the_delete(c, ids):
    order_id, _, remnant_lot = _remnant_order(c, ids)
    with flask_app.app_context():
        db.session.get(MaterialPrice, remnant_lot).stock_quantity = 0   # the remnant sheet went out
        db.session.commit()
    page = _delete(c, order_id)
    assert 'Стомана 2мм - остатък' in page and 'вече е използван' in page
    assert _get(Order, order_id).deleted_at is None
    assert _get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(9)
    assert _get(Detail, ids['detail']).stock_quantity == 5


def test_client_detail_below_zero_refuses_the_delete(c, ids):
    order_id = _order(c, [{'type': 'detail', 'id': ids['private'], 'qty': 2}], client_id=str(ids['owner']))
    (item,) = _items(order_id)
    assert _produce(c, 'item', item.id, 1)['status'] == 'success'
    with flask_app.app_context():
        db.session.get(Detail, ids['private']).stock_quantity = 0   # handed over to the client already
        db.session.commit()
    page = _delete(c, order_id)
    assert 'клиентска партида' in page and 'Планка клиент' in page
    assert _get(Order, order_id).deleted_at is None
    assert _get(MaterialPrice, ids['lot']).stock_quantity == pytest.approx(0.9)


def test_non_admin_cannot_edit_or_delete(c, ids):
    order_id = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 2}])
    for username in ('qa_worker', 'qa_customer'):
        other = _login(username)
        assert other.post(f'/admin/orders/{order_id}/delete').status_code == 302
        assert other.get(f'/admin/orders/{order_id}/edit').status_code == 302
        other.post(f'/admin/orders/{order_id}/edit', data={'customer_name': 'Хакер'})
        assert _get(Order, order_id) is not None and _get(Order, order_id).customer_name == 'QA'


# ---------------------------------------------------------------- edit

def test_quantity_change_respects_produced_and_keeps_price(c, ids):
    with flask_app.app_context():
        db.session.get(MaterialPrice, ids['ours']).stock_quantity = 0.8
        db.session.commit()
    order_id = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 5},
                          {'type': 'product', 'id': ids['product'], 'qty': 1}])
    line, product_line = _items(order_id)
    assert _reserved(order_id) == {ids['ours']: pytest.approx(0.7)}   # 5 x 0.1 + 2 components x 0.1
    assert not _all(MaterialRequest)
    price = line.unit_price
    assert _produce(c, 'item', line.id, 2)['status'] == 'success'   # lot 0.6 left, 0.5 still reserved

    page = c.post(f'/admin/orders/{order_id}/edit', data={'customer_name': 'QA', f'qty_{line.id}': '1'},
                  follow_redirects=True).get_data(as_text=True)
    assert 'под 2 бр.' in page and _items(order_id)[0].quantity_ordered == 5

    # detail line down 0.2, product line up 0.4: still reserved 0.7 > 0.6 on hand -> one request
    c.post(f'/admin/orders/{order_id}/edit', data={'customer_name': 'QA Нов', f'qty_{line.id}': '3',
                                                    f'qty_{product_line.id}': '3', 'client_id': str(ids['owner'])})
    line, product_line = _items(order_id)
    assert (line.quantity_ordered, line.unit_price) == (3, price)
    (component,) = _all(OrderItemComponent)
    assert component.quantity_needed == 6
    order = _get(Order, order_id)
    assert (order.customer_name, order.client_id, order.status) == ('QA Нов', ids['owner'], 'in_production')
    assert _reserved(order_id) == {ids['ours']: pytest.approx(0.9)}
    (request,) = _all(MaterialRequest)
    assert request.material_id == ids['ours'] and request.quantity == pytest.approx(0.1)

    c.post(f'/admin/orders/{order_id}/edit', data={'customer_name': 'QA Нов', f'qty_{line.id}': '2',
                                                    'client_id': str(ids['owner'])})
    assert _reserved(order_id) == {ids['ours']: pytest.approx(0.8)}
    assert len(_all(MaterialRequest)) == 1   # shrinking never asks for material
    assert _get(Order, order_id).status == 'in_production'


def test_added_detail_line_is_priced_like_create_order(c, ids):
    row = {'type': 'detail', 'id': ids['detail'], 'qty': 2, 'own_material': True,
           'operations': [{'service_id': ids['service'], 'duration_minutes': 3, 'description': 'ръб'}]}
    reference = _order(c, [row], client_id=str(ids['owner']))
    order_id = _order(c, [{'type': 'product', 'id': ids['product'], 'qty': 1}], client_id=str(ids['owner']))
    page = c.post(f'/admin/orders/{order_id}/lines/add', data={
        'item': f'detail:{ids["detail"]}', 'qty': '2', 'own_material': '1',
        'op_service_id': [str(ids['service']), ''], 'op_duration': ['3', ''], 'op_description': ['ръб', '']},
        follow_redirects=True).get_data(as_text=True)
    assert 'Добавен ред' in page
    added = _items(order_id)[-1]
    (expected,) = _items(reference)
    assert (added.detail_id, added.quantity_ordered, added.own_material) == (ids['detail'], 2, True)
    assert added.unit_price == expected.unit_price
    assert [(o.service_id, o.duration_minutes, o.description) for o in _all(appmod.OrderItemOperation, order_item_id=added.id)] \
        == [(ids['service'], 3, 'ръб')]
    assert _reserved(order_id) == {ids['ours']: pytest.approx(0.2)}   # own material takes nothing from a lot


def test_added_product_line_gets_its_components(c, ids):
    order_id = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 1}])
    report = c.get('/admin/production').get_data(as_text=True)
    assert f'/admin/orders/{order_id}/edit' in report and f'/admin/orders/{order_id}/delete' in report
    page = c.get(f'/admin/orders/{order_id}/edit').get_data(as_text=True)
    assert 'Нов ред' in page and f'value="product:{ids["product"]}"' in page and 'Огъване' in page
    c.post(f'/admin/orders/{order_id}/lines/add', data={'item': f'product:{ids["product"]}', 'qty': '3'})
    added = _items(order_id)[-1]
    (component,) = _all(OrderItemComponent, order_item_id=added.id)
    assert (added.product_id, component.detail_id, component.quantity_needed) == (ids['product'], ids['detail'], 6)
    assert _reserved(order_id) == {ids['ours']: pytest.approx(0.7)}


def test_remove_line_rules(c, ids):
    order_id = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 2},
                          {'type': 'product', 'id': ids['product'], 'qty': 1},
                          {'type': 'detail', 'id': ids['detail'], 'qty': 4}])
    produced, untouched, last = _items(order_id)
    _produce(c, 'item', produced.id, 1)
    page = c.post(f'/admin/order-items/{produced.id}/delete', follow_redirects=True).get_data(as_text=True)
    assert 'вече има производство' in page and len(_items(order_id)) == 3
    c.post(f'/admin/order-items/{untouched.id}/delete')
    assert [i.id for i in _items(order_id)] == [produced.id, last.id] and not _all(OrderItemComponent)
    assert _reserved(order_id) == {ids['ours']: pytest.approx(0.6)}
    c.post(f'/admin/order-items/{last.id}/delete')
    page = c.post(f'/admin/order-items/{produced.id}/delete', follow_redirects=True).get_data(as_text=True)
    assert 'вече има производство' in page and [i.id for i in _items(order_id)] == [produced.id]


def test_order_without_reservation_stays_without(c, ids):
    customer = _login('qa_customer')
    order_id = _order(customer, [{'type': 'detail', 'id': ids['detail'], 'qty': 2}])
    assert not _reserved(order_id)
    admin = _login('qa_admin')
    (line,) = _items(order_id)
    admin.post(f'/admin/orders/{order_id}/edit', data={'customer_name': 'QA', f'qty_{line.id}': '200'})
    admin.post(f'/admin/orders/{order_id}/lines/add', data={'item': f'detail:{ids["detail"]}', 'qty': '300'})
    assert _items(order_id)[0].quantity_ordered == 200 and len(_items(order_id)) == 2
    assert not _reserved(order_id) and not _all(MaterialRequest)


def test_cancelled_order_is_not_editable(c, ids):
    order_id = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 2}])
    c.post(f'/orders/{order_id}/cancel')
    (line,) = _items(order_id)
    page = c.post(f'/admin/orders/{order_id}/edit', data={'customer_name': 'QA', f'qty_{line.id}': '9'},
                  follow_redirects=True).get_data(as_text=True)
    assert 'отказана' in page and _items(order_id)[0].quantity_ordered == 2
    assert c.get(f'/admin/orders/{order_id}/edit').status_code == 302


def test_refresh_prices_after_a_catalog_change(c, ids):
    """"Обнови" on the order edit page: an unchanged line shows nothing; after the detail's price changes the page
    shows "сега:" and refreshing one line / all lines sets unit_price to the price a new order would get (same
    client terms, own material and order-time operations kept). Never applied by itself."""
    row = {'type': 'detail', 'id': ids['detail'], 'qty': 2, 'own_material': False,
           'operations': [{'service_id': ids['service'], 'duration_minutes': 3}]}
    order_id = _order(c, [row, {'type': 'product', 'id': ids['product'], 'qty': 1}], client_id=str(ids['owner']))
    detail_line, product_line = _items(order_id)
    assert 'сега:' not in c.get(f'/admin/orders/{order_id}/edit').get_data(as_text=True)
    db.session.get(Detail, ids['detail']).calculated_price = 8.0   # the fixture's session = the requests' one
    db.session.commit()
    page = c.get(f'/admin/orders/{order_id}/edit').get_data(as_text=True)
    assert page.count('сега:') == 2 and 'Обнови всички цени (2)' in page
    assert _get(OrderItem, detail_line.id).unit_price == detail_line.unit_price          # not applied by itself
    c.post(f'/admin/orders/{order_id}/refresh-prices', data={'item_id': str(detail_line.id)})
    reference = _order(c, [row], client_id=str(ids['owner']))
    (expected,) = _items(reference)
    assert _get(OrderItem, detail_line.id).unit_price == expected.unit_price != detail_line.unit_price
    assert _get(OrderItem, product_line.id).unit_price == product_line.unit_price        # only that line
    c.post(f'/admin/orders/{order_id}/refresh-prices', data={})
    assert _get(OrderItem, product_line.id).unit_price != product_line.unit_price
    assert 'сега:' not in c.get(f'/admin/orders/{order_id}/edit').get_data(as_text=True)
    g.pop('_login_user', None)
    assert _login('qa_worker').post(f'/admin/orders/{order_id}/refresh-prices').status_code in (302, 403)


def test_order_request_file_create_edit_delete(c, ids):
    """The client's request file of an order (owner's decision 2026-11-20): attached on create, shown on the report
    and served to staff / the order's own account, replaced or removed on edit (old file deleted), a disallowed
    extension refused, removed with a hard-deleted order."""
    import io
    folder = flask_app.config['ORDER_ITEM_FILE_FOLDER']
    g.pop('_login_user', None)
    before = {o.id for o in _all(Order)}
    c.post('/orders/new', data={'customer_name': 'QA', 'client_id': '',
                                'cart_json': json.dumps([{'type': 'detail', 'id': ids['detail'], 'qty': 1}]),
                                'request_file': (io.BytesIO(b'%PDF-1.4 req'), 'Искане 5.pdf')},
           content_type='multipart/form-data')
    (order_id,) = {o.id for o in _all(Order)} - before
    order = _get(Order, order_id)
    first = order.request_file
    assert order.request_file_name == 'Искане 5.pdf' and os.path.exists(os.path.join(folder, first))
    assert 'Искане от клиента: Искане 5.pdf' in c.get('/admin/production').get_data(as_text=True)
    res = c.get(f'/orders/{order_id}/request-file')
    assert res.status_code == 200 and res.data == b'%PDF-1.4 req'
    res.close()
    g.pop('_login_user', None)
    assert _login('qa_customer').get(f'/orders/{order_id}/request-file').status_code == 404   # not his order

    g.pop('_login_user', None)
    form = {'customer_name': 'QA'}
    c.post(f'/admin/orders/{order_id}/edit', data={**form, 'request_file': (io.BytesIO(b'x'), 'virus.exe')},
           content_type='multipart/form-data')
    assert _get(Order, order_id).request_file == first                                         # refused
    c.post(f'/admin/orders/{order_id}/edit', data={**form, 'request_file': (io.BytesIO(b'new'), 'new.xlsx')},
           content_type='multipart/form-data')
    second = _get(Order, order_id).request_file
    assert second != first and not os.path.exists(os.path.join(folder, first))
    c.post(f'/admin/orders/{order_id}/edit', data={**form, 'request_file_remove': '1'})
    assert _get(Order, order_id).request_file is None and not os.path.exists(os.path.join(folder, second))

    c.post(f'/admin/orders/{order_id}/edit', data={**form, 'request_file': (io.BytesIO(b'3'), 'third.pdf')},
           content_type='multipart/form-data')
    third = _get(Order, order_id).request_file
    _delete(c, order_id)
    assert not os.path.exists(os.path.join(folder, third))
