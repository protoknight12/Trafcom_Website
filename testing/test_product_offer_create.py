"""
pytest: "Създай оферта" from a product (admin_product_offer_create()) saves a real Offer through _save_offer()
with one product line at the print view's price for the picked client; the print view itself saves nothing;
a worker is refused.

Run with:
    pytest testing/test_product_offer_create.py -v
"""
import os

os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, Product, ProductDetail,
                  Offer, OfferItem, ActivityLog, calculate_product_pricing, limiter)


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add_all([
            User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'),
            User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker'),
        ])
        material = MaterialPrice(key='qa_mat', display_name='QA Material', cost_per_m2=10)
        db.session.add(material)
        db.session.flush()
        detail = Detail(name='QA Detail', material_key=material.key, width=10, height=10,
                        total_length=1, pierce_count=1, calculated_price=7.333)
        product = Product(name='QA Product', markup_percent=15)
        client = Client(name='QA Client', product_adjustment_type='discount', product_adjustment_percent=7)
        db.session.add_all([detail, product, client])
        db.session.flush()
        db.session.add(ProductDetail(product_id=product.id, detail_id=detail.id, quantity=3))
        db.session.commit()
        yield product.id, client.id
        db.session.remove()
        db.drop_all()


def _login(username):
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


def test_create_offer_from_product_for_client(ids):
    product_id, client_id = ids
    res = _login('qa_admin').post(f'/admin/products/{product_id}/offer/create', data={'client_id': str(client_id)})
    assert res.status_code == 302
    with flask_app.app_context():
        offer = Offer.query.one()
        assert res.headers['Location'].endswith(f'/admin/offers?open={offer.id}')
        assert offer.client_id == client_id and offer.object_title == 'QA Product'
        item = OfferItem.query.filter_by(offer_id=offer.id).one()
        product, client = db.session.get(Product, product_id), db.session.get(Client, client_id)
        expected = round(calculate_product_pricing(product, client)['sell_price'], 2)
        assert expected != round(calculate_product_pricing(product)['sell_price'], 2)   # the client's terms count
        assert (item.item_type, item.product_id, item.quantity, item.unit, item.unit_price) == \
            ('product', product_id, 1, 'бр', expected)
        assert ActivityLog.query.filter(ActivityLog.action.contains(offer.number)).count() >= 1


def test_create_offer_without_client_takes_list_price(ids):
    product_id, _ = ids
    c = _login('qa_admin')
    c.post(f'/admin/products/{product_id}/offer/create', data={'client_id': ''})
    c.post(f'/admin/products/{product_id}/offer/create', data={})
    with flask_app.app_context():
        offers = Offer.query.order_by(Offer.id).all()
        assert len(offers) == 2 and offers[0].number != offers[1].number
        assert all(o.client_id is None for o in offers)
        list_price = round(calculate_product_pricing(db.session.get(Product, product_id))['sell_price'], 2)
        assert all(i.unit_price == list_price for i in OfferItem.query.all())


def test_print_view_saves_nothing_and_offers_both(ids):
    product_id, _ = ids
    res = _login('qa_admin').get(f'/admin/products/{product_id}/offer')
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert f'/admin/products/{product_id}/offer/create' in html and 'Само за печат' in html
    with flask_app.app_context():
        assert Offer.query.count() == 0


def test_worker_refused(ids):
    product_id, client_id = ids
    _login('qa_worker').post(f'/admin/products/{product_id}/offer/create', data={'client_id': str(client_id)})
    with flask_app.app_context():
        assert Offer.query.count() == 0
