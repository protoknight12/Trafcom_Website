"""
pytest regression test for Operation.description - an optional free-text note
distinguishing operations that share a Service but mean different things in
practice (e.g. "лазерно рязане" in-house vs. "външно лазерно рязане"
outsourced), added alongside the operations-total/grand-total summary on
detail_dxf_dashboard.html. Uses the Flask test client (same pattern as
test_quick_create_product_components.py) since this needs real
request/session/login behavior.

Run with:
    pytest testing/test_operation_description.py -v
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

import json

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, MaterialPrice, Detail, Service, Operation, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        material = MaterialPrice(key='qa_mat', display_name='QA Material', cost_per_m2=10, cutting_speed_mm_per_min=1, pierce_rate_per_min=0.1)
        service = Service(name='QA Laser', price_per_hour_eur=60)
        db.session.add_all([admin, material, service])
        db.session.flush()
        detail = Detail(name='QA Detail', material_key=material.key, width=10, height=10,
                         total_length=1, pierce_count=1, calculated_price=20.0)
        db.session.add(detail)
        db.session.commit()
        yield flask_app, detail.id, service.id
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    flask_app, detail_id, service_id = app
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c, detail_id, service_id


def test_add_operation_stores_description(client):
    c, detail_id, service_id = client
    rows = [{'service_id': service_id, 'duration_minutes': 30, 'description': 'външно лазерно рязане'}]
    res = c.post(f'/admin/details/{detail_id}/operations/add', data={'operations_json': json.dumps(rows)})
    assert res.status_code == 302
    with flask_app.app_context():
        op = Operation.query.filter_by(detail_id=detail_id).first()
        assert op is not None
        assert op.description == 'външно лазерно рязане'


def test_add_operation_blank_description_stored_as_none(client):
    c, detail_id, service_id = client
    rows = [{'service_id': service_id, 'duration_minutes': 10, 'description': '   '}]
    res = c.post(f'/admin/details/{detail_id}/operations/add', data={'operations_json': json.dumps(rows)})
    assert res.status_code == 302
    with flask_app.app_context():
        op = Operation.query.filter_by(detail_id=detail_id).first()
        assert op.description is None


def test_details_page_shows_operations_total_and_grand_total(client):
    c, detail_id, service_id = client
    rows = [
        {'service_id': service_id, 'duration_minutes': 30, 'description': 'external'},  # 30 EUR
        {'service_id': service_id, 'duration_minutes': 15},  # 15 EUR
    ]
    c.post(f'/admin/details/{detail_id}/operations/add', data={'operations_json': json.dumps(rows)})
    res = c.get(f'/details/{detail_id}/files')
    assert res.status_code == 200
    html = res.data.decode()
    assert '45.00' in html  # operations-only total (30 + 15)
    assert '65.00' in html  # grand total (20 base + 45 operations)
