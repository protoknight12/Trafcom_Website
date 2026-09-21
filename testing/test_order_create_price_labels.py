"""
pytest regression test: every price a linked client sees on /orders/new must
already reflect their own Client's discount/markup - not just the numbers
actually charged (already covered by test_catalog_ownership.py's order-POST
tests), but every *displayed* label too. Guards a real miss: the per-detail
"operations" picker's <option> text (order_create.html's #pending_op_service_id)
kept showing the raw Service.price_per_hour_eur even after the JS cost
calculation itself (pendingOpCost()) was already client-adjusted - the label
and the actual charge disagreed. See client_service_rate()/_viewer_client().

Run with:
    pytest testing/test_order_create_price_labels.py -v
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

from app import app as flask_app, db, User, Client, Service, ClientServicePrice, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()

        client = Client(name='QA Discount Client')
        db.session.add(client)
        db.session.flush()

        user = User(username='qa_client', password=generate_password_hash('irrelevant123'),
                    role='regular_user', client_id=client.id)
        db.session.add(user)

        service = Service(name='QA Лазерно рязане', price_per_hour_eur=60.0)
        db.session.add(service)
        db.session.flush()
        db.session.add(ClientServicePrice(client_id=client.id, service_id=service.id,
                                           adjustment_type='discount', adjustment_percent=25))

        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client_browser(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_client', 'password': 'irrelevant123'})
    return c


def test_operations_picker_option_shows_discounted_rate(client_browser):
    body = client_browser.get('/orders/new').get_data(as_text=True)
    # 60.00 EUR/h with a 25% client discount -> 45.00, never the raw 60.00.
    assert '45.00' in body
    assert '60.00 €/' not in body, "the operations-picker <option> label must never show the raw undiscounted rate"
