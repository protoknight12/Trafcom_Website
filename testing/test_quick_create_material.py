"""
pytest regression test for api_quick_create_material() - the new endpoint
backing the "material" quick-create modal used from the quick-create-detail
modal and the delivery-note detail-material picker. Mirrors
admin_add_material()'s validation (required prices, duplicate display_name
check, ERP № conflict check) but as a JSON AJAX endpoint. Uses the Flask
test client (same pattern as test_security_fixes.py) since this needs real
request/session/login behavior.

Run with:
    pytest testing/test_quick_create_material.py -v
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

from app import app as flask_app, db, User, MaterialPrice, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        worker = User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker')
        db.session.add_all([admin, worker])
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def admin_client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c


@pytest.fixture
def worker_client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_worker', 'password': 'irrelevant123'})
    return c


def test_quick_create_material_full_fields(admin_client):
    res = admin_client.post('/api/quick-create-material', data={
        'display_name': 'QA Титан', 'type': 'sheets', 'brand': 'QA-Brand',
        'cost_per_m2': '100', 'cutting_speed_mm_per_min': '5', 'pierce_rate_per_min': '0.5',
        'sheet_length_mm': '2000', 'sheet_width_mm': '1000', 'thickness_mm': '2',
    })
    assert res.status_code == 200
    body = res.get_json()
    assert body['status'] == 'success'
    with flask_app.app_context():
        m = MaterialPrice.query.filter_by(display_name='QA Титан').first()
        assert m is not None
        assert m.cost_per_m2 == 100 and m.brand == 'QA-Brand' and m.sheet_length_mm == 2000
        assert m.erp_number is not None  # auto-generated


def test_quick_create_material_requires_prices(admin_client):
    res = admin_client.post('/api/quick-create-material', data={'display_name': 'QA No Price'})
    assert res.status_code == 400
    assert res.get_json()['status'] == 'error'
    with flask_app.app_context():
        assert MaterialPrice.query.filter_by(display_name='QA No Price').first() is None


def test_quick_create_material_rejects_duplicate_name(admin_client):
    data = {'display_name': 'QA Dup', 'cost_per_m2': '1', 'cutting_speed_mm_per_min': '1', 'pierce_rate_per_min': '0.1'}
    first = admin_client.post('/api/quick-create-material', data=data)
    assert first.get_json()['status'] == 'success'
    second = admin_client.post('/api/quick-create-material', data=data)
    assert second.status_code == 400
    assert second.get_json()['status'] == 'error'


def test_quick_create_material_forbidden_for_worker(worker_client):
    res = worker_client.post('/api/quick-create-material', data={
        'display_name': 'QA Worker Material', 'cost_per_m2': '1', 'cutting_speed_mm_per_min': '1', 'pierce_rate_per_min': '0.1',
    })
    assert res.status_code == 403
