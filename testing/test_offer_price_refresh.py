"""
pytest: the offer editor's "Обнови" - each saved product/detail line carries the server's CURRENT price for the
offer's client (current_price, from detail_price_for() / calculate_product_pricing()), so the editor can tell a line
whose catalog price moved since it was quoted. Text lines and lines without a catalog row carry none. The refresh
itself is a click in the editor (nothing here changes a stored price), and the two ways to an order stay as they
were: from an offer the QUOTED line price (minus the offer discount) is frozen, a direct order takes the detail's
fresh price with the client's terms.

Run with:
    pytest testing/test_offer_price_refresh.py -v
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
from werkzeug.security import generate_password_hash

import app as appmod
from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, Product, ProductDetail, Offer, OfferItem,
                 Order, OrderItem, limiter, seed_material_types)


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        client = Client(name='Клиент А', detail_adjustment_type='discount', detail_adjustment_percent=10)
        db.session.add_all([admin, client, MaterialPrice(key='steel', display_name='Стомана', cost_per_m2=10, type='sheets',
                                                         cutting_speed_mm_per_min=1000, pierce_rate_per_min=10)])
        db.session.flush()
        detail = Detail(name='Планка', material_key='steel', width=100, height=100, total_length=1, pierce_count=1,
                        calculated_price=10.0)
        product = Product(name='Изделие', markup_percent=0)
        db.session.add_all([detail, product])
        db.session.flush()
        db.session.add(ProductDetail(product_id=product.id, detail_id=detail.id, quantity=2))
        offer = Offer(number='00000000777', created_by_id=admin.id, client_id=client.id, discount_percent=10)
        db.session.add(offer)
        db.session.flush()
        # quoted today: the detail at the client's 9.00 (10 - 10%), the product at 20.00, plus a note
        db.session.add_all([
            OfferItem(offer_id=offer.id, position=0, item_type='detail', detail_id=detail.id, name='Планка',
                      quantity=3, unit='бр', unit_price=9.0),
            OfferItem(offer_id=offer.id, position=1, item_type='product', product_id=product.id, name='Изделие',
                      quantity=1, unit='бр', unit_price=20.0),
            OfferItem(offer_id=offer.id, position=2, item_type='text', name='Транспорт', quantity=1, unit='бр',
                      unit_price=50.0)])
        db.session.commit()
        yield dict(offer=offer.id, client=client.id, detail=detail.id, product=product.id)
        db.session.remove()
        db.drop_all()


def _login():
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c


def _lines(ids):
    """{item_type: (unit_price, current_price)} as the editor receives them"""
    with flask_app.app_context():
        offer = db.session.get(Offer, ids['offer'])
        return {row['type']: (row['unit_price'], row['current_price']) for row in appmod._offer_items_json(offer)}


def _set_detail_price(ids, price):
    with flask_app.app_context():
        db.session.get(Detail, ids['detail']).calculated_price = price
        db.session.commit()


def test_payload_carries_the_current_price_per_line(ids):
    assert _lines(ids) == {'detail': (9.0, 9.0), 'product': (20.0, 20.0), 'text': (50.0, None)}
    page = _login().get(f'/admin/offers/{ids["offer"]}/edit').get_data(as_text=True)
    assert '"current_price"' in page and 'refreshPrices' in page


def test_changed_detail_price_is_stale_for_detail_and_product_lines(ids):
    _set_detail_price(ids, 20.0)
    lines = _lines(ids)
    assert lines['detail'] == (9.0, 18.0)       # 20 - the client's 10%
    assert lines['product'] == (20.0, 40.0)     # 2 x 20
    assert lines['text'] == (50.0, None)        # a note never shows it


def test_changed_client_terms_are_stale(ids):
    with flask_app.app_context():
        db.session.get(Client, ids['client']).detail_adjustment_percent = 20
        db.session.commit()
    assert _lines(ids)['detail'] == (9.0, 8.0)
    assert _lines(ids)['product'] == (20.0, 20.0)   # product terms are another setting


def test_offer_without_client_is_priced_at_list(ids):
    _set_detail_price(ids, 20.0)
    with flask_app.app_context():
        db.session.get(Offer, ids['offer']).client_id = None
        db.session.commit()
    assert _lines(ids)['detail'] == (9.0, 20.0)


def test_missing_catalog_row_has_no_current_price(ids):
    with flask_app.app_context():
        item = OfferItem.query.filter_by(offer_id=ids['offer'], item_type='detail').one()
        item.detail_id = 999999
        db.session.commit()
    assert _lines(ids)['detail'] == (9.0, None)


def test_editor_marks_an_offer_that_already_has_an_order(ids):
    c = _login()
    assert 'OFFER_HAS_ORDER = false' in c.get(f'/admin/offers/{ids["offer"]}/edit').get_data(as_text=True)
    with flask_app.app_context():
        order = Order(order_number='ORD-9', user_id=User.query.one().id, customer_name='QA', status='new')
        db.session.add(order)
        db.session.flush()
        db.session.get(Offer, ids['offer']).order_id = order.id
        db.session.commit()
    db.session.remove()   # the request shares the fixture's app context - drop its stale Offer
    assert 'OFFER_HAS_ORDER = true' in c.get(f'/admin/offers/{ids["offer"]}/edit').get_data(as_text=True)


def test_order_from_offer_keeps_quoted_price_direct_order_takes_fresh_one(ids):
    _set_detail_price(ids, 20.0)   # after the quote
    c = _login()
    with flask_app.app_context():
        item_id = OfferItem.query.filter_by(offer_id=ids['offer'], item_type='detail').one().id
    c.post(f'/admin/offers/{ids["offer"]}/create-order', data={'customer_name': 'QA', 'item_ids': [str(item_id)]})
    with flask_app.app_context():
        (line,) = OrderItem.query.all()
        assert line.unit_price == 8.1           # quoted 9.00 less the offer's 10% - not today's 18.00

    g.pop('_login_user', None)
    c.post('/orders/new', data={'customer_name': 'QA', 'client_id': str(ids['client']),
                                'cart_json': json.dumps([{'type': 'detail', 'id': ids['detail'], 'qty': 1}])})
    with flask_app.app_context():
        direct = OrderItem.query.order_by(OrderItem.id.desc()).first()
        assert direct.id != line.id and direct.unit_price == 18.0   # fresh list price with the client's terms
