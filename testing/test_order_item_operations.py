"""
pytest regression test for the order-time operations picker added to
create_order() - see OrderItemOperation and _order_item_operations_cost() in
app.py. A standalone-detail cart line can carry its own {service_id,
duration_minutes} rows (independent of any Operation permanently attached to
the Detail catalog row); their cost must be folded into OrderItem.unit_price
once, at creation time, and invalid rows must be skipped rather than failing
the whole order. Uses the Flask test client (same pattern as
test_quick_create_product_components.py) since this needs real
request/session/login behavior.

Run with:
    pytest testing/test_order_item_operations.py -v
"""
import atexit
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

import json

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, MaterialPrice, Detail, Service, Order, OrderItem, OrderItemOperation, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        # A regular_user, not admin/worker - the operations picker is open to
        # anyone placing an order, not gated like the quick-create-detail modal.
        user = User(username='qa_user', password=generate_password_hash('irrelevant123'), role='regular_user')
        db.session.add(user)
        material = MaterialPrice(key='qa_mat', display_name='QA Material', cost_per_m2=10,
                                  cutting_speed_mm_per_min=1, pierce_rate_per_min=0.1)
        db.session.add(material)
        cutting = Service(name='Рязане', price_per_hour_eur=60.0)   # 1 €/min
        bending = Service(name='Огъване', price_per_hour_eur=120.0)  # 2 €/min
        db.session.add_all([cutting, bending])
        db.session.flush()
        hinge = Detail(name='Алуминиева панта', material_key=material.key, width=10, height=10,
                        total_length=1, pierce_count=1, calculated_price=5.0)
        db.session.add(hinge)
        db.session.commit()
        yield flask_app, hinge.id, cutting.id, bending.id
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    flask_app, hinge_id, cutting_id, bending_id = app
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_user', 'password': 'irrelevant123'})
    return c, hinge_id, cutting_id, bending_id


def test_operations_cost_folded_into_unit_price(client):
    c, hinge_id, cutting_id, bending_id = client
    cart = [{
        'type': 'detail', 'id': hinge_id, 'qty': 3,
        'operations': [
            {'service_id': cutting_id, 'duration_minutes': 10},   # 10 €
            {'service_id': bending_id, 'duration_minutes': 5},    # 10 €
        ],
    }]
    res = c.post('/orders/new', data={'customer_name': 'QA Customer', 'cart_json': json.dumps(cart)})
    assert res.status_code == 302

    with flask_app.app_context():
        order = Order.query.filter_by(customer_name='QA Customer').first()
        item = OrderItem.query.filter_by(order_id=order.id).first()
        # base 5.0 + (10 min @ 1 €/min) + (5 min @ 2 €/min) = 5 + 10 + 10 = 25
        assert item.unit_price == 25.0
        assert item.line_total == 75.0  # 25 * qty(3), operations priced per unit

        ops = OrderItemOperation.query.filter_by(order_item_id=item.id).order_by(OrderItemOperation.sequence).all()
        assert [o.service_id for o in ops] == [cutting_id, bending_id]
        assert [o.sequence for o in ops] == [0, 1]


def test_invalid_operation_rows_are_skipped(client):
    c, hinge_id, cutting_id, _bending_id = client
    cart = [{
        'type': 'detail', 'id': hinge_id, 'qty': 1,
        'operations': [
            {'service_id': cutting_id, 'duration_minutes': 10},  # valid: 10 €
            {'service_id': 999999, 'duration_minutes': 10},      # unknown service
            {'service_id': cutting_id, 'duration_minutes': 0},   # non-positive duration
            {'service_id': cutting_id, 'duration_minutes': -5},  # negative duration
        ],
    }]
    res = c.post('/orders/new', data={'customer_name': 'QA Customer 2', 'cart_json': json.dumps(cart)})
    assert res.status_code == 302

    with flask_app.app_context():
        order = Order.query.filter_by(customer_name='QA Customer 2').first()
        item = OrderItem.query.filter_by(order_id=order.id).first()
        assert item.unit_price == 15.0  # base 5.0 + only the one valid 10 € op
        assert OrderItemOperation.query.filter_by(order_item_id=item.id).count() == 1


def test_detail_without_operations_keeps_base_price(client):
    c, hinge_id, _cutting_id, _bending_id = client
    cart = [{'type': 'detail', 'id': hinge_id, 'qty': 2}]
    res = c.post('/orders/new', data={'customer_name': 'QA Customer 3', 'cart_json': json.dumps(cart)})
    assert res.status_code == 302

    with flask_app.app_context():
        order = Order.query.filter_by(customer_name='QA Customer 3').first()
        item = OrderItem.query.filter_by(order_id=order.id).first()
        assert item.unit_price == 5.0
        assert OrderItemOperation.query.filter_by(order_item_id=item.id).count() == 0
