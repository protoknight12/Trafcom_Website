"""
pytest regression test for admin_production_report()'s POST handler bumping
Detail.stock_quantity by the produced-quantity delta - a produced piece is a
piece that now physically exists, so recording it should land in stock the
same way a delivery note does (see _bump_stock()), without a separate manual
step. Covers both target_type branches (standalone-detail OrderItem and a
product's OrderItemComponent), and that a downward correction removes stock
again rather than only ever adding.

Run with:
    pytest testing/test_production_stock_bump.py -v
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

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, MaterialPrice, Detail, Product, ProductDetail,
                  Order, OrderItem, OrderItemComponent, limiter)


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        db.session.add(admin)
        material = MaterialPrice(key='qa_mat', display_name='QA Material', cost_per_m2=10,
                                  cutting_speed_mm_per_min=1, pierce_rate_per_min=0.1)
        db.session.add(material)
        db.session.flush()
        detail = Detail(name='QA Detail', material_key=material.key, width=10, height=10,
                         total_length=1, pierce_count=1, calculated_price=5.0, stock_quantity=0)
        product = Product(name='QA Product', markup_percent=0)
        db.session.add_all([detail, product])
        db.session.flush()
        db.session.add(ProductDetail(product_id=product.id, detail_id=detail.id, quantity=1))
        db.session.commit()

        order = Order(order_number='ORD-QA-1', user_id=admin.id, customer_name='QA', status='new')
        db.session.add(order)
        db.session.flush()
        standalone_item = OrderItem(order_id=order.id, detail_id=detail.id, quantity_ordered=100, unit_price=5.0)
        product_item = OrderItem(order_id=order.id, product_id=product.id, quantity_ordered=10, unit_price=5.0)
        db.session.add_all([standalone_item, product_item])
        db.session.flush()
        component = OrderItemComponent(order_item_id=product_item.id, detail_id=detail.id,
                                        detail_name_snapshot=detail.name, quantity_needed=10)
        db.session.add(component)
        db.session.commit()

        yield flask_app, detail.id, standalone_item.id, component.id
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    flask_app, detail_id, item_id, component_id = app
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c, detail_id, item_id, component_id


def test_producing_a_standalone_detail_bumps_stock(client):
    c, detail_id, item_id, _component_id = client
    res = c.post('/admin/production', data={'target_type': 'item', 'target_id': item_id, 'produced_qty': '20'})
    assert res.status_code == 200
    with flask_app.app_context():
        assert Detail.query.get(detail_id).stock_quantity == 20


def test_correcting_produced_qty_downward_removes_stock_again(client):
    c, detail_id, item_id, _component_id = client
    c.post('/admin/production', data={'target_type': 'item', 'target_id': item_id, 'produced_qty': '20'})
    c.post('/admin/production', data={'target_type': 'item', 'target_id': item_id, 'produced_qty': '5'})
    with flask_app.app_context():
        assert Detail.query.get(detail_id).stock_quantity == 5


def test_producing_a_product_component_bumps_the_components_detail_stock(client):
    c, detail_id, _item_id, component_id = client
    res = c.post('/admin/production', data={'target_type': 'component', 'target_id': component_id, 'produced_qty': '3'})
    assert res.status_code == 200
    with flask_app.app_context():
        assert Detail.query.get(detail_id).stock_quantity == 3


def test_repeated_updates_accumulate_by_delta_not_overwrite(client):
    c, detail_id, item_id, _component_id = client
    c.post('/admin/production', data={'target_type': 'item', 'target_id': item_id, 'produced_qty': '10'})
    c.post('/admin/production', data={'target_type': 'item', 'target_id': item_id, 'produced_qty': '30'})
    with flask_app.app_context():
        assert Detail.query.get(detail_id).stock_quantity == 30
