"""
pytest regression test: an Offer linked to a Client should have that
client's Детайли discount/markup applied when converted into a real Order
(admin_offer_create_order()) - same client-aware pricing a self-service
order already gets (see calculate_product_pricing()/detail_price_for()).
Also covers _offer_picker_context()'s client_pricing payload, used by
admin_offer_edit.html's JS to pre-fill a freshly-picked catalog row's price -
this only ever affects a NEW/not-yet-saved price; an already-saved
OfferItem.unit_price is a frozen column untouched by a later catalog price
or client discount/percent change.

Run with:
    pytest testing/test_offer_client_pricing.py -v
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

from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, Product,
                  Offer, OfferItem, Order, OrderItem, _offer_picker_context, limiter)


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        client = Client(name='QA Discount Client', detail_adjustment_type='discount', detail_adjustment_percent=20)
        db.session.add_all([admin, client])
        material = MaterialPrice(key='qa_mat', display_name='QA Mat', type='sheets', cost_per_m2=10.0,
                                  cutting_speed_mm_per_min=1000, pierce_rate_per_min=30)
        db.session.add(material)
        db.session.flush()
        detail = Detail(name='QA Detail', material_key=material.key, width=100, height=100,
                         total_length=0, pierce_count=0, calculated_price=20.0)
        db.session.add(detail)
        db.session.commit()

        offer = Offer(number='00000000901', created_by_id=admin.id, client_id=client.id)
        db.session.add(offer)
        db.session.flush()
        # A stale/negotiated frozen price on the offer itself - must never
        # leak into the freshly-computed order price.
        offer_item = OfferItem(offer_id=offer.id, position=0, item_type='detail', detail_id=detail.id,
                                name=detail.name, quantity=1, unit='бр', unit_price=777.0)
        db.session.add(offer_item)
        db.session.commit()

        yield flask_app, offer.id, offer_item.id, client.id, detail.id
        db.session.remove()
        db.drop_all()


@pytest.fixture
def admin_client(app):
    flask_app, offer_id, offer_item_id, client_id, detail_id = app
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c, offer_id, offer_item_id, client_id, detail_id


def test_offer_create_order_applies_linked_clients_discount(admin_client):
    c, offer_id, offer_item_id, client_id, detail_id = admin_client
    res = c.post(f'/admin/offers/{offer_id}/create-order', data={
        'customer_name': 'QA Discount Client', 'item_ids': [str(offer_item_id)],
    })
    assert res.status_code == 302
    with flask_app.app_context():
        order = Order.query.filter_by(customer_name='QA Discount Client').first()
        item = OrderItem.query.filter_by(order_id=order.id).one()
        assert item.unit_price == 16.0, "20 EUR list price with a 20% client discount -> 16.0, not the offer's frozen 777.0"


def test_offer_picker_context_exposes_client_detail_terms(app):
    flask_app, offer_id, offer_item_id, client_id, detail_id = app
    with flask_app.app_context():
        products, details, clients, client_pricing = _offer_picker_context()
        assert client_pricing[client_id]['detail_type'] == 'discount'
        assert client_pricing[client_id]['detail_percent'] == 20
        detail_row = next(d for d in details if d.get('id') == detail_id)
        assert detail_row['price'] == 20.0, "picker's base price is the undiscounted list price - JS applies the discount"


def test_offer_edit_page_renders_with_client_pricing(admin_client):
    c, offer_id, offer_item_id, client_id, detail_id = admin_client
    res = c.get(f'/admin/offers/{offer_id}/edit')
    assert res.status_code == 200
    res_new = c.get('/admin/offers/new')
    assert res_new.status_code == 200
