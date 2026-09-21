"""
pytest regression test for per-client catalog ownership/visibility
(Detail.owner_client_id / Product.owner_client_id, _catalog_item_visible()):
a client (regular_user linked to a Client) must only ever see/order Details
and Products that are public (owner_client_id is NULL) or owned by their own
Client - never another client's private catalog entry. Staff (admin/worker)
always see the full, unfiltered catalog. Also covers detail_dxf_dashboard()'s
matching access check.

Run with:
    pytest testing/test_catalog_ownership.py -v
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

from app import app as flask_app, db, User, Client, MaterialPrice, Detail, Product, OrderItem, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()

        client_a = Client(name='Клиент А')
        # A large, obvious discount - used to prove a spoofed client_id in
        # the order POST can't make client A's order get priced as client B.
        client_b = Client(name='Клиент Б', detail_adjustment_type='discount', detail_adjustment_percent=50)
        db.session.add_all([client_a, client_b])
        db.session.flush()

        user_a = User(username='qa_client_a', password=generate_password_hash('irrelevant123'),
                      role='regular_user', client_id=client_a.id)
        user_unlinked = User(username='qa_unlinked', password=generate_password_hash('irrelevant123'),
                              role='regular_user')
        worker = User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker')
        db.session.add_all([user_a, user_unlinked, worker])

        material = MaterialPrice(key='qa_mat', display_name='QA Mat', type='sheets', cost_per_m2=10.0,
                                  cutting_speed_mm_per_min=1000, pierce_rate_per_min=30)
        db.session.add(material)
        db.session.flush()

        public_detail = Detail(name='Публичен детайл', material_key=material.key, width=100, height=100,
                                total_length=0, pierce_count=0, calculated_price=5.0, owner_client_id=None)
        private_a_detail = Detail(name='Частен детайл А', material_key=material.key, width=100, height=100,
                                   total_length=0, pierce_count=0, calculated_price=7.0, owner_client_id=client_a.id)
        private_b_detail = Detail(name='Частен детайл Б', material_key=material.key, width=100, height=100,
                                   total_length=0, pierce_count=0, calculated_price=9.0, owner_client_id=client_b.id)
        db.session.add_all([public_detail, private_a_detail, private_b_detail])

        public_product = Product(name='Публичен продукт', markup_percent=0, owner_client_id=None)
        private_b_product = Product(name='Частен продукт Б', markup_percent=0, owner_client_id=client_b.id)
        db.session.add_all([public_product, private_b_product])

        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client_a_browser(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_client_a', 'password': 'irrelevant123'})
    return c


@pytest.fixture
def unlinked_browser(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_unlinked', 'password': 'irrelevant123'})
    return c


@pytest.fixture
def worker_browser(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_worker', 'password': 'irrelevant123'})
    return c


def _ids(app):
    with flask_app.app_context():
        return {
            'public_detail': Detail.query.filter_by(name='Публичен детайл').first().id,
            'private_a_detail': Detail.query.filter_by(name='Частен детайл А').first().id,
            'private_b_detail': Detail.query.filter_by(name='Частен детайл Б').first().id,
            'public_product': Product.query.filter_by(name='Публичен продукт').first().id,
            'private_b_product': Product.query.filter_by(name='Частен продукт Б').first().id,
        }


def test_client_catalog_hides_other_clients_private_items(app, client_a_browser):
    ids = _ids(app)
    body = client_a_browser.get('/orders/new').get_data(as_text=True)
    assert 'Публичен детайл' in body
    assert 'Частен детайл А' in body
    assert 'Частен детайл Б' not in body, "client A must never see client B's private detail"
    assert 'Публичен продукт' in body
    assert 'Частен продукт Б' not in body


def test_unlinked_user_sees_only_public_items(app, unlinked_browser):
    body = unlinked_browser.get('/orders/new').get_data(as_text=True)
    assert 'Публичен детайл' in body
    assert 'Частен детайл А' not in body
    assert 'Частен детайл Б' not in body


def test_staff_sees_full_unfiltered_catalog(app, worker_browser):
    body = worker_browser.get('/orders/new').get_data(as_text=True)
    assert 'Публичен детайл' in body
    assert 'Частен детайл А' in body
    assert 'Частен детайл Б' in body


def test_ordering_another_clients_private_detail_is_rejected_server_side(app, client_a_browser):
    """Even a hand-crafted cart_json bypassing the UI must not let client A
    order client B's private detail - the visibility check runs server-side
    in create_order(), not just in what the GET page renders."""
    ids = _ids(app)
    cart = [{'type': 'detail', 'id': ids['private_b_detail'], 'qty': 1}]
    res = client_a_browser.post('/orders/new', data={'customer_name': 'QA', 'cart_json': json.dumps(cart)},
                                 follow_redirects=True)
    assert res.status_code == 200
    with flask_app.app_context():
        assert OrderItem.query.count() == 0, "no order item should have been created for an invisible detail"


def test_ordering_own_private_detail_succeeds(app, client_a_browser):
    ids = _ids(app)
    cart = [{'type': 'detail', 'id': ids['private_a_detail'], 'qty': 1}]
    res = client_a_browser.post('/orders/new', data={'customer_name': 'QA', 'cart_json': json.dumps(cart)},
                                 follow_redirects=True)
    assert res.status_code == 200
    with flask_app.app_context():
        assert OrderItem.query.count() == 1


def test_detail_dashboard_blocks_non_owner_client(app, client_a_browser):
    ids = _ids(app)
    res = client_a_browser.get(f"/details/{ids['private_b_detail']}/files", follow_redirects=True)
    assert res.status_code == 200
    assert 'Нямате достъп' in res.get_data(as_text=True)


def test_detail_dashboard_allows_owner_client(app, client_a_browser):
    ids = _ids(app)
    res = client_a_browser.get(f"/details/{ids['private_a_detail']}/files")
    assert res.status_code == 200


def test_detail_dashboard_allows_public_detail_for_any_client(app, client_a_browser):
    ids = _ids(app)
    res = client_a_browser.get(f"/details/{ids['public_detail']}/files")
    assert res.status_code == 200


def test_detail_dashboard_allows_staff_regardless_of_owner(app, worker_browser):
    ids = _ids(app)
    res = worker_browser.get(f"/details/{ids['private_b_detail']}/files")
    assert res.status_code == 200


# --- /orders/new client picker - a regular user may only ever order as
# their own linked client, never pick/spoof another one (see the
# "provera dali klientskiya dropdown ..." follow-up: Client now carries
# pricing terms and catalog ownership, so letting a self-service order pick
# any existing Client is a real discount/visibility bypass, not just a
# billing-name nicety). ---

def test_order_page_only_offers_own_client_to_linked_user(app, client_a_browser):
    body = client_a_browser.get('/orders/new').get_data(as_text=True)
    assert 'Клиент А' in body
    assert 'Клиент Б' not in body, "another client's name must not even be listed for a linked regular user"


def test_order_page_client_pricing_json_excludes_other_clients(app, client_a_browser):
    """The embedded CLIENT_PRICING JS blob used to leak every client's
    discount/markup terms to any logged-in viewer - must now only ever
    contain the viewer's own client (or nothing, for staff-free viewers)."""
    ids = _ids(app)
    with flask_app.app_context():
        client_b_id = Client.query.filter_by(name='Клиент Б').first().id
    body = client_a_browser.get('/orders/new').get_data(as_text=True)
    assert f'"{client_b_id}"' not in body or 'CLIENT_PRICING' not in body.split(f'"{client_b_id}"')[0][-200:], \
        "client B's id must not appear as a CLIENT_PRICING key on client A's page"


def test_staff_still_sees_every_client_on_order_page(app, worker_browser):
    body = worker_browser.get('/orders/new').get_data(as_text=True)
    assert 'Клиент А' in body
    assert 'Клиент Б' in body


def test_unlinked_user_sees_no_client_options(app, unlinked_browser):
    body = unlinked_browser.get('/orders/new').get_data(as_text=True)
    assert 'Клиент А' not in body
    assert 'Клиент Б' not in body


def test_spoofed_client_id_in_order_post_is_ignored_for_regular_user(app, client_a_browser):
    """Client A (no discount of their own) posts an order but claims
    client_id=Client B (50% discount) in the form data - the server must
    still price/attribute the order as client A, not fall for the spoof."""
    ids = _ids(app)
    with flask_app.app_context():
        client_b_id = Client.query.filter_by(name='Клиент Б').first().id
    cart = [{'type': 'detail', 'id': ids['public_detail'], 'qty': 1}]
    res = client_a_browser.post('/orders/new', data={
        'customer_name': 'Клиент Б', 'client_id': str(client_b_id), 'cart_json': json.dumps(cart)
    }, follow_redirects=True)
    assert res.status_code == 200
    with flask_app.app_context():
        order_item = OrderItem.query.one()
        assert order_item.unit_price == 5.0, "must be the undiscounted public_detail price, not 2.5 (50% off)"
        assert order_item.order.client_id != client_b_id, "order must not be attributed to the spoofed client"


def test_quick_create_client_links_unlinked_regular_user(app, unlinked_browser):
    res = unlinked_browser.post('/api/quick-create-client', data={'name': 'Нов клиент от чекаут'})
    assert res.status_code == 200
    with flask_app.app_context():
        user = User.query.filter_by(username='qa_unlinked').first()
        new_client = Client.query.filter_by(name='Нов клиент от чекаут').first()
        assert new_client is not None
        assert user.client_id == new_client.id, "a still-unlinked self-service user must get auto-linked"


def test_quick_create_client_does_not_relink_already_linked_user(app, client_a_browser):
    res = client_a_browser.post('/api/quick-create-client', data={'name': 'Друга фирма'})
    assert res.status_code == 200
    with flask_app.app_context():
        user = User.query.filter_by(username='qa_client_a').first()
        client_a = Client.query.filter_by(name='Клиент А').first()
        assert user.client_id == client_a.id, "an already-linked user must keep their own client, not the new one"
