"""
pytest regression test for api_quick_create_product() accepting a
components_json BOM (detail_id + quantity pairs) at creation time, instead
of always creating a bare product with 0 details. Uses the Flask test
client (same pattern as test_security_fixes.py) since this needs real
request/session/login behavior.

Run with:
    pytest testing/test_quick_create_product_components.py -v
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

from app import app as flask_app, db, User, MaterialPrice, Detail, ProductDetail, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        db.session.add(admin)
        material = MaterialPrice(key='qa_mat', display_name='QA Material', cost_per_m2=10, cost_per_meter_cut=1, cost_per_pierce=0.1)
        db.session.add(material)
        db.session.flush()
        detail_a = Detail(name='Detail A', material_key=material.key, width=10, height=10,
                           total_length=1, pierce_count=1, calculated_price=5.0)
        detail_b = Detail(name='Detail B', material_key=material.key, width=10, height=10,
                           total_length=1, pierce_count=1, calculated_price=7.5)
        db.session.add_all([detail_a, detail_b])
        db.session.commit()
        yield flask_app, detail_a.id, detail_b.id
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    flask_app, detail_a_id, detail_b_id = app
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c, detail_a_id, detail_b_id


def test_quick_create_product_attaches_components(client):
    c, detail_a_id, detail_b_id = client
    components = [{'detail_id': detail_a_id, 'quantity': 2}, {'detail_id': detail_b_id, 'quantity': 1}]
    res = c.post('/api/quick-create-product', data={
        'name': 'QA Product', 'description': '', 'markup_percent': '0',
        'components_json': json.dumps(components),
    })
    assert res.status_code == 200
    body = res.get_json()
    assert body['status'] == 'success'
    product_id = body['product']['id']

    with flask_app.app_context():
        rows = ProductDetail.query.filter_by(product_id=product_id).all()
        by_detail = {r.detail_id: r.quantity for r in rows}
        assert by_detail == {detail_a_id: 2, detail_b_id: 1}
    # price must reflect the attached components, not stay at 0 like a bare product
    assert body['product']['price'] > 0


def test_quick_create_product_merges_duplicate_detail_ids(client):
    c, detail_a_id, _detail_b_id = client
    components = [{'detail_id': detail_a_id, 'quantity': 2}, {'detail_id': detail_a_id, 'quantity': 3}]
    res = c.post('/api/quick-create-product', data={
        'name': 'QA Product Dup', 'description': '', 'markup_percent': '0',
        'components_json': json.dumps(components),
    })
    body = res.get_json()
    assert body['status'] == 'success'
    with flask_app.app_context():
        rows = ProductDetail.query.filter_by(product_id=body['product']['id']).all()
        assert len(rows) == 1 and rows[0].quantity == 5


def test_quick_create_product_rejects_unknown_detail_id(client):
    c, _detail_a_id, _detail_b_id = client
    res = c.post('/api/quick-create-product', data={
        'name': 'QA Product Bad', 'description': '', 'markup_percent': '0',
        'components_json': json.dumps([{'detail_id': 999999, 'quantity': 1}]),
    })
    assert res.status_code == 400
    assert res.get_json()['status'] == 'error'


def test_quick_create_product_with_no_components_stays_bare(client):
    c, _detail_a_id, _detail_b_id = client
    res = c.post('/api/quick-create-product', data={'name': 'QA Bare Product', 'description': '', 'markup_percent': '0'})
    body = res.get_json()
    assert body['status'] == 'success'
    assert body['product']['price'] == 0
