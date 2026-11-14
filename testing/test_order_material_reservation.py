"""
pytest: an Order reserves its raw material when it is placed (OrderMaterialReservation, from
create_order() for staff and admin_offer_create_order(); a customer's order only after staff press
admin_confirm_order_material()), coverage is computed live, first come first
served (_open_material_reservations()), a shortage at creation raises one MaterialRequest per short lot
naming the order (not for a client's own lot), production on an uncovered lot is refused
(admin_production_report(), increases only), consumption shrinks what is still reserved, a cancelled
order releases its reservation and a delivery note covers a waiting order without extra code.

Run with:
    pytest testing/test_order_material_reservation.py -v
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

from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, Product, ProductDetail, Order,
                 OrderItem, OrderItemComponent, OrderMaterialReservation, MaterialRequest, Offer, OfferItem,
                 limiter, seed_material_types, _order_material_shortages)


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        owner = Client(name='Клиент А')
        db.session.add_all([admin, owner,
                            User(username='qa_customer', password=generate_password_hash('irrelevant123'), role='regular_user')])
        db.session.flush()
        # cost 0 = never priced: the delivery note test bumps this very lot instead of splitting a new one
        ours = MaterialPrice(key='steel', display_name='Стомана 2мм', cost_per_m2=0, type='sheets', thickness_mm=2.0,
                             sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=1)
        lot = MaterialPrice(key='steel_c', display_name='Стомана клиент', cost_per_m2=0, type='sheets',
                            sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=0.1, owner_client_id=owner.id)
        db.session.add_all([ours, lot])
        db.session.flush()
        # 500 x 400 mm = 0.2 m² per piece = 0.1 of a 2 m² sheet
        detail = Detail(name='Планка', material_key='steel', width=500, height=400, total_length=1,
                        pierce_count=1, calculated_price=5.0, stock_quantity=0)
        private = Detail(name='Планка клиент', material_key='steel_c', width=500, height=400, total_length=1,
                         pierce_count=1, calculated_price=0.0, stock_quantity=0, owner_client_id=owner.id)
        product = Product(name='Изделие', markup_percent=0)
        db.session.add_all([detail, private, product])
        db.session.flush()
        db.session.add(ProductDetail(product_id=product.id, detail_id=detail.id, quantity=2))
        db.session.commit()
        yield dict(admin=admin.id, owner=owner.id, ours=ours.id, lot=lot.id, detail=detail.id,
                   private=private.id, product=product.id)
        db.session.remove()
        db.drop_all()


@pytest.fixture
def c(ids):
    client = flask_app.test_client()
    client.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return client


@pytest.fixture
def customer(ids):
    client = _as(flask_app.test_client())
    client.post('/login', data={'username': 'qa_customer', 'password': 'irrelevant123'})
    return client


def _as(client):
    """The fixtures keep one app context open, so flask-login's cached user (g) would leak between two
    test clients - drop it before switching between the admin and the customer client."""
    from flask import g
    g.pop('_login_user', None)
    return client


def _order(c, cart, client_id=''):
    """Places an order through create_order(); returns (order id, redirect response)."""
    _as(c)
    before = {o.id for o in _all(Order)}
    res = c.post('/orders/new', data={'customer_name': 'QA', 'cart_json': json.dumps(cart), 'client_id': client_id})
    assert res.status_code == 302
    (new_id,) = {o.id for o in _all(Order)} - before
    return new_id, res


def _all(model, **filters):
    with flask_app.app_context():
        return model.query.filter_by(**filters).all()


def _reserved(order_id):
    with flask_app.app_context():
        return {r.material_id: r.quantity for r in OrderMaterialReservation.query.filter_by(order_id=order_id)}


def _shortages(order_id):
    with flask_app.app_context():
        return [(s['material'].id, round(s['missing'], 9)) for s in _order_material_shortages(db.session.get(Order, order_id))]


def _item(order_id):
    with flask_app.app_context():
        return OrderItem.query.filter_by(order_id=order_id).first().id


def _produce(c, item_id, qty, target_type='item'):
    return _as(c).post('/admin/production', data={'target_type': target_type, 'target_id': item_id, 'produced_qty': str(qty)})


def _flashes(c):
    with c.session_transaction() as sess:
        return [m for _, m in sess.get('_flashes', [])]


def test_create_order_reserves_product_and_detail_lines(c, ids):
    order_id, _ = _order(c, [{'type': 'product', 'id': ids['product'], 'qty': 2},          # 4 pieces = 0.4 sheet
                             {'type': 'detail', 'id': ids['detail'], 'qty': 3},            # 0.3 sheet
                             {'type': 'detail', 'id': ids['detail'], 'qty': 50, 'own_material': True}])  # none
    assert _reserved(order_id) == pytest.approx({ids['ours']: 0.7})
    assert _shortages(order_id) == []
    assert _all(MaterialRequest) == []


def test_offer_create_order_reserves_too(c, ids):
    with flask_app.app_context():
        offer = Offer(number='00000000901', created_by_id=ids['admin'])
        db.session.add(offer)
        db.session.flush()
        lines = [OfferItem(offer_id=offer.id, position=0, item_type='product', product_id=ids['product'],
                           name='Изделие', quantity=1, unit='бр', unit_price=10.0),
                 OfferItem(offer_id=offer.id, position=1, item_type='detail', detail_id=ids['detail'],
                           name='Планка', quantity=4, unit='бр', unit_price=5.0)]
        db.session.add_all(lines)
        db.session.commit()
        offer_id, line_ids = offer.id, [str(line.id) for line in lines]
    res = c.post(f'/admin/offers/{offer_id}/create-order', data={'customer_name': 'QA', 'item_ids': line_ids})
    assert res.status_code == 302
    (order,) = _all(Order)
    assert _reserved(order.id) == pytest.approx({ids['ours']: 0.6})   # 2 + 4 pieces


def test_fifo_second_order_short_and_one_request_raised(c, ids):
    first, _ = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 7}])     # 0.7 of the 1 sheet
    assert _all(MaterialRequest) == []
    second, _ = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 7}])    # 0.3 left -> 0.4 missing
    assert _shortages(first) == []
    assert _shortages(second) == [(ids['ours'], pytest.approx(0.4))]
    (req,) = _all(MaterialRequest)
    assert req.material_id == ids['ours'] and req.quantity == pytest.approx(0.4) and req.status == 'new'
    with flask_app.app_context():
        number = db.session.get(Order, second).order_number
    assert number in req.note
    assert any('не стига' in m and 'заявка' in m for m in _flashes(c))


def test_production_blocked_until_covered_and_corrections_always_allowed(c, ids):
    first, _ = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 7}])
    second, _ = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 7}])
    res = _produce(c, _item(second), 1)
    assert res.status_code == 400 and 'не стига' in res.get_json()['message']
    with flask_app.app_context():
        assert db.session.get(OrderItem, _item(second)).quantity_produced == 0
        assert db.session.get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(1)
    assert _produce(c, _item(first), 3).get_json()['status'] == 'success'

    with flask_app.app_context():   # made earlier, before it was blocked
        db.session.get(OrderItem, _item(second)).quantity_produced = 2
        db.session.commit()
    assert _produce(c, _item(second), 1).get_json()['status'] == 'success'   # downward: never blocked


def test_consumption_shrinks_the_reservation(c, ids):
    with flask_app.app_context():
        db.session.get(MaterialPrice, ids['ours']).stock_quantity = 1.4   # exactly both orders
        db.session.commit()
    first, _ = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 7}])
    second, _ = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 7}])
    assert _produce(c, _item(first), 5).get_json()['status'] == 'success'   # stock 0.9, first still holds 0.2
    with flask_app.app_context():
        lots = _order_material_shortages(db.session.get(Order, second))
    assert lots == []                                                       # 0.9 - 0.2 = 0.7 free for the second
    assert _produce(c, _item(second), 7).get_json()['status'] == 'success'
    assert _produce(c, _item(first), 7).get_json()['status'] == 'success'   # its remaining 0.2 was still there


def test_cancel_releases_and_unblocks_the_next_order(c, ids):
    first, _ = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 7}])
    second, _ = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 7}])
    assert _produce(c, _item(second), 1).status_code == 400
    assert c.post(f'/orders/{first}/cancel').status_code == 302
    assert _shortages(second) == []
    assert _produce(c, _item(second), 7).get_json()['status'] == 'success'


def test_client_lot_short_raises_no_request_but_blocks(c, ids):
    order_id, _ = _order(c, [{'type': 'detail', 'id': ids['private'], 'qty': 5}], client_id=str(ids['owner']))
    assert _reserved(order_id) == pytest.approx({ids['lot']: 0.5})
    assert _shortages(order_id) == [(ids['lot'], pytest.approx(0.4))]
    assert _all(MaterialRequest) == []
    assert any('Клиентът трябва да го донесе' in m for m in _flashes(c))
    res = _produce(c, _item(order_id), 1)
    assert res.status_code == 400 and 'не стига' in res.get_json()['message']


def test_delivery_note_covers_a_waiting_order(c, ids):
    first, _ = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 7}])
    second, _ = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 7}])
    assert _produce(c, _item(second), 1).status_code == 400
    line = {'type': 'material', 'name': 'Стомана 2мм', 'material_key': 'steel', 'qty': 0.4, 'unit_price': 40,
            'width': 1000, 'height': 2000, 'thickness': 2.0, 'material_type': 'sheets'}
    assert c.post('/admin/delivery-notes/create', data={'items_json': json.dumps([line])}).status_code == 302
    with flask_app.app_context():
        assert db.session.get(MaterialPrice, ids['ours']).stock_quantity == pytest.approx(1.4)
    assert _shortages(second) == []
    assert _produce(c, _item(second), 7).get_json()['status'] == 'success'


def test_customer_order_reserves_nothing_until_staff_confirm(c, customer, ids):
    order_id, _ = _order(customer, [{'type': 'detail', 'id': ids['detail'], 'qty': 100000}])   # far beyond the stock
    assert _reserved(order_id) == {} and _all(MaterialRequest) == []
    assert 'Потвърди и задели материал' in _as(c).get('/admin/production').get_data(as_text=True)
    assert _produce(c, _item(order_id), 1).get_json()['status'] == 'success'   # never blocked without rows

    other, _ = _order(customer, [{'type': 'detail', 'id': ids['detail'], 'qty': 5}])
    res = _as(c).post(f'/admin/orders/{other}/confirm-material')
    assert res.status_code == 302
    assert _reserved(other) == pytest.approx({ids['ours']: 0.5})
    assert _flashes(c)[0] == f'Материалът за поръчка {_number(other)} е заделен.'
    html = _as(c).get('/admin/production').get_data(as_text=True)
    assert html.count('Материал заделен') == 1


def test_staff_order_still_reserves_at_creation(c, ids):
    order_id, _ = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 3}])
    assert _reserved(order_id) == pytest.approx({ids['ours']: 0.3})


def test_confirm_raises_request_on_shortage_and_twice_is_a_noop(c, customer, ids):
    order_id, _ = _order(customer, [{'type': 'detail', 'id': ids['detail'], 'qty': 15}])   # 1.5 sheets, 1 in stock
    assert _as(c).post(f'/admin/orders/{order_id}/confirm-material').status_code == 302
    (req,) = _all(MaterialRequest)
    assert req.material_id == ids['ours'] and req.quantity == pytest.approx(0.5) and _number(order_id) in req.note
    assert _reserved(order_id) == pytest.approx({ids['ours']: 1.5})
    assert any('не стига' in m for m in _flashes(c))
    from app import ActivityLog
    with flask_app.app_context():
        assert ActivityLog.query.filter(ActivityLog.action.like('%Потвърден и заделен материал%')).count() == 1
    assert _as(c).post(f'/admin/orders/{order_id}/confirm-material').status_code == 302
    assert len(_all(MaterialRequest)) == 1 and _reserved(order_id) == pytest.approx({ids['ours']: 1.5})
    assert any('вече е заделен' in m for m in _flashes(c))


def test_confirm_refused_for_non_staff_and_closed_orders(c, customer, ids):
    order_id, _ = _order(customer, [{'type': 'detail', 'id': ids['detail'], 'qty': 5}])
    assert _as(customer).post(f'/admin/orders/{order_id}/confirm-material').status_code in (302, 403)
    assert _reserved(order_id) == {}
    for status in ('completed', 'cancelled'):
        with flask_app.app_context():
            db.session.get(Order, order_id).status = status
            db.session.commit()
        assert _as(c).post(f'/admin/orders/{order_id}/confirm-material').status_code == 302
        assert _reserved(order_id) == {}


def _number(order_id):
    with flask_app.app_context():
        return db.session.get(Order, order_id).order_number


def test_production_report_and_storage_show_the_reservation(c, ids):
    first, _ = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 7}])
    second, _ = _order(c, [{'type': 'detail', 'id': ids['detail'], 'qty': 7}])
    html = _as(c).get('/admin/production').get_data(as_text=True)
    assert html.count('Материал заделен') == 1 and html.count('Чака материал:') == 1
    assert 'Заделено: 1.4' in c.get('/storage/materials').get_data(as_text=True)
