"""
pytest regression test for create_production_order()'s stock-clamping rule:
unlike delivery notes/orders (which never block on stock - see
order_missing_items()), a planned production job must never be allowed to
send the chosen material batch below zero. Requesting more than the stock
covers must clamp the quantity down to the largest piece count that fits
(floor(stock / per-piece need)) with a warning flash, not just warn-and-allow
the original request; a genuine zero-stock case must refuse to create a job
at all. Uses the Flask test client (same pattern as
test_quick_create_material.py) since this needs real request/redirect/flash
behavior.

Run with:
    pytest testing/test_production_order_clamp.py -v
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

from app import app as flask_app, db, User, MaterialPrice, Detail, ProductionOrder, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        worker = User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker')
        db.session.add(worker)
        # 1000mm x 500mm piece = 0.5 m^2/piece, sheet stock tracked in m^2.
        material = MaterialPrice(key='qa_sheet', display_name='QA Ламарина', cost_per_m2=10.0,
                                  cutting_speed_mm_per_min=1000, pierce_rate_per_min=30, type='sheets',
                                  stock_quantity=1.2)
        db.session.add(material)
        db.session.flush()
        detail = Detail(name='QA Detail', material_key=material.key, width=1000.0, height=500.0,
                         total_length=0.0, pierce_count=0, calculated_price=5.0)
        db.session.add(detail)
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def worker_client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_worker', 'password': 'irrelevant123'})
    return c


def _ids(app):
    with flask_app.app_context():
        m = MaterialPrice.query.filter_by(key='qa_sheet').first()
        d = Detail.query.filter_by(name='QA Detail').first()
        return d.id, m.id


def test_quantity_within_stock_is_unchanged(app, worker_client):
    detail_id, material_id = _ids(app)
    res = worker_client.post('/admin/production-orders/create',
                              data={'detail_id': detail_id, 'material_id': material_id, 'quantity': 2},
                              follow_redirects=True)
    assert res.status_code == 200
    with flask_app.app_context():
        job = ProductionOrder.query.one()
        assert job.quantity == 2
        assert job.planned_material_qty == 1.0  # 2 x 0.5 m^2, fits in the 1.2 m^2 stock


def test_over_request_clamps_to_max_affordable(app, worker_client):
    detail_id, material_id = _ids(app)
    # 1.2 m^2 stock / 0.5 m^2 per piece = floor(2.4) = 2 pieces max, not the 5 requested.
    res = worker_client.post('/admin/production-orders/create',
                              data={'detail_id': detail_id, 'material_id': material_id, 'quantity': 5},
                              follow_redirects=True)
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert 'alert-warning' in body, "a clamp must surface as a yellow warning banner"
    with flask_app.app_context():
        job = ProductionOrder.query.one()
        assert job.quantity == 2, "clamped down to the largest quantity that doesn't push stock negative"
        assert job.planned_material_qty == 1.0
        assert job.material.stock_quantity == 1.2, "creating a job never touches stock itself - only completing one does"


def test_zero_affordable_quantity_refuses_to_create_job(app, worker_client):
    detail_id, material_id = _ids(app)
    with flask_app.app_context():
        MaterialPrice.query.filter_by(key='qa_sheet').first().stock_quantity = 0.1  # less than one 0.5 m^2 piece
        db.session.commit()

    res = worker_client.post('/admin/production-orders/create',
                              data={'detail_id': detail_id, 'material_id': material_id, 'quantity': 1},
                              follow_redirects=True)
    assert res.status_code == 200
    assert 'alert-warning' in res.get_data(as_text=True)
    with flask_app.app_context():
        assert ProductionOrder.query.count() == 0, "not even a 1-piece job when stock can't cover a single piece"
