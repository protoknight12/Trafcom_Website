"""
pytest regression test for admin_offer_create_order() - turning checked
product/detail lines of an Offer into a real Order (see admin_offer_edit.html's
per-row checkboxes). Uses the Flask test client since this needs real
request/session/login behavior, same pattern as
test_quick_create_product_components.py.

Run with:
    pytest testing/test_offer_create_order.py -v
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
                  Offer, OfferItem, Order, OrderItem, OrderItemComponent, limiter)


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
                         total_length=1, pierce_count=1, calculated_price=7.5)
        db.session.add(detail)
        db.session.flush()
        product = Product(name='QA Product', markup_percent=0)
        db.session.add(product)
        db.session.flush()
        db.session.add(ProductDetail(product_id=product.id, detail_id=detail.id, quantity=2))
        db.session.commit()

        offer = Offer(number='00000000900', created_by_id=admin.id)
        db.session.add(offer)
        db.session.flush()
        product_item = OfferItem(offer_id=offer.id, position=0, item_type='product', product_id=product.id,
                                  name=product.name, quantity=3, unit='бр', unit_price=999.0)
        detail_item = OfferItem(offer_id=offer.id, position=1, item_type='detail', detail_id=detail.id,
                                 name=detail.name, quantity=4, unit='бр', unit_price=999.0)
        text_item = OfferItem(offer_id=offer.id, position=2, item_type='text', name='Транспорт',
                               quantity=1, unit='бр', unit_price=50.0)
        db.session.add_all([product_item, detail_item, text_item])
        db.session.commit()

        yield flask_app, offer.id, product_item.id, detail_item.id, text_item.id, product.id, detail.id
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    flask_app, offer_id, product_item_id, detail_item_id, text_item_id, product_id, detail_id = app
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c, offer_id, product_item_id, detail_item_id, text_item_id, product_id, detail_id


def test_create_order_from_selected_offer_items(client):
    c, offer_id, product_item_id, detail_item_id, text_item_id, product_id, detail_id = client
    res = c.post(f'/admin/offers/{offer_id}/create-order', data={
        'customer_name': 'QA Customer',
        'item_ids': [str(product_item_id), str(detail_item_id), str(text_item_id)],
    })
    assert res.status_code == 302

    with flask_app.app_context():
        order = Order.query.filter_by(customer_name='QA Customer').first()
        assert order is not None
        items = OrderItem.query.filter_by(order_id=order.id).all()
        # the text line has no product_id/detail_id and must be skipped even
        # though its id was submitted
        assert len(items) == 2

        product_line = next(i for i in items if i.product_id == product_id)
        assert product_line.quantity_ordered == 3
        # price must come from the current catalog, not the offer's frozen 999.0
        assert product_line.unit_price != 999.0
        components = OrderItemComponent.query.filter_by(order_item_id=product_line.id).all()
        assert len(components) == 1 and components[0].detail_id == detail_id
        assert components[0].quantity_needed == 2 * 3  # ProductDetail quantity x ordered qty

        detail_line = next(i for i in items if i.detail_id == detail_id)
        assert detail_line.quantity_ordered == 4
        assert detail_line.unit_price == 7.5  # Detail.calculated_price


def test_create_order_requires_customer_name(client):
    c, offer_id, product_item_id, _detail_item_id, _text_item_id, _product_id, _detail_id = client
    res = c.post(f'/admin/offers/{offer_id}/create-order', data={
        'customer_name': '', 'item_ids': [str(product_item_id)],
    }, follow_redirects=True)
    assert res.status_code == 200
    with flask_app.app_context():
        assert Order.query.count() == 0


def test_create_order_rejects_only_text_items(client):
    c, offer_id, _product_item_id, _detail_item_id, text_item_id, _product_id, _detail_id = client
    res = c.post(f'/admin/offers/{offer_id}/create-order', data={
        'customer_name': 'QA Customer 2', 'item_ids': [str(text_item_id)],
    }, follow_redirects=True)
    assert res.status_code == 200
    with flask_app.app_context():
        assert Order.query.count() == 0
