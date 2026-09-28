"""
pytest: api_offer_item_to_product() - a free-text offer line becomes a
catalog Product (priced via one ProductExtraCost = the line's price) so
admin_offer_create_order() can put it on an order; a same-named product
(case-insensitive, Cyrillic too) is reused instead of duplicated.

Run with:
    pytest testing/test_offer_item_to_product.py -v
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
        pass  # Windows keeps the file locked while SQLAlchemy's pooled connection is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Offer, OfferItem, Product, OrderItem, limiter


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def _offer_with_text_line(name='Стойка специална', qty=3, price=120.0):
    offer = Offer(number='T1', created_by_id=User.query.first().id)
    item = OfferItem(offer=offer, position=0, item_type='text', name=name, quantity=qty, unit_price=price)
    db.session.add_all([offer, item])
    db.session.commit()
    return offer, item


def test_convert_saved_line_creates_priced_product_and_orders(admin_client):
    offer, item = _offer_with_text_line()
    r = admin_client.post('/api/offer-item-to-product', data={
        'name': item.name, 'quantity': '3', 'unit_price': '120', 'description_html': '<b>боядисана</b>',
        'offer_item_id': str(item.id)}).get_json()
    assert r['status'] == 'success' and r['created'] and r['product']['price'] == 120
    db.session.refresh(item)
    assert item.item_type == 'product' and item.product_id == r['product']['id']
    assert Product.query.get(item.product_id).description == 'боядисана'

    admin_client.post(f'/admin/offers/{offer.id}/create-order', data={'customer_name': 'X', 'item_ids': [str(item.id)]})
    oi = OrderItem.query.one()
    assert oi.product_id == item.product_id and oi.unit_price == 120 and oi.quantity_ordered == 3


def test_same_name_links_existing_product(admin_client):
    existing = Product(name='Стойка специална', markup_percent=0)
    db.session.add(existing)
    db.session.commit()
    r = admin_client.post('/api/offer-item-to-product', data={
        'name': '  стойка СПЕЦИАЛНА ', 'quantity': '1', 'unit_price': '5'}).get_json()
    assert r['status'] == 'success' and not r['created'] and r['product']['id'] == existing.id
    assert Product.query.count() == 1


def test_rejects_line_without_qty_or_price(admin_client):
    for data in ({'name': 'Бележка'}, {'name': 'Бележка', 'quantity': '0', 'unit_price': '5'}, {'name': '', 'quantity': '1', 'unit_price': '5'}):
        assert admin_client.post('/api/offer-item-to-product', data=data).status_code == 400
    assert Product.query.count() == 0
