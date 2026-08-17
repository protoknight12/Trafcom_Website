"""
pytest regression test for the material-request dashboard (create_material_request /
update_material_request) - batch-creating requests from the "new request" page
(items_json, one or more {material_id, quantity} lines), and the
new/processing/ordered status flow where 'ordered' requires a supplier.

Run with:
    pytest testing/test_material_requests.py -v
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

from app import app as flask_app, db, User, MaterialPrice, MaterialRequest, Supplier, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        worker = User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker')
        material = MaterialPrice(key='qa_steel', display_name='QA Стомана', cost_per_m2=10)
        material2 = MaterialPrice(key='qa_alu', display_name='QA Алуминий', cost_per_m2=20)
        supplier = Supplier(name='QA Доставчик ЕООД')
        db.session.add_all([worker, material, material2, supplier])
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def worker_client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_worker', 'password': 'irrelevant123'})
    return c


def test_create_material_request(worker_client, app):
    with flask_app.app_context():
        material_id = MaterialPrice.query.filter_by(key='qa_steel').first().id
    items = [{'material_id': material_id, 'quantity': 5}]
    res = worker_client.post('/storage/requests/create', data={'items_json': json.dumps(items)})
    assert res.status_code == 302
    with flask_app.app_context():
        req = MaterialRequest.query.first()
        assert req is not None
        assert req.quantity == 5 and req.status == 'new' and req.material_id == material_id


def test_create_material_request_batch_multiple_materials(worker_client, app):
    with flask_app.app_context():
        steel_id = MaterialPrice.query.filter_by(key='qa_steel').first().id
        alu_id = MaterialPrice.query.filter_by(key='qa_alu').first().id
    items = [{'material_id': steel_id, 'quantity': 5}, {'material_id': alu_id, 'quantity': 2.5}]
    res = worker_client.post('/storage/requests/create', data={'items_json': json.dumps(items)})
    assert res.status_code == 302
    with flask_app.app_context():
        reqs = MaterialRequest.query.order_by(MaterialRequest.material_id).all()
        assert len(reqs) == 2
        assert {r.material_id for r in reqs} == {steel_id, alu_id}
        assert all(r.status == 'new' for r in reqs)


def test_create_material_request_rejects_missing_quantity(worker_client, app):
    with flask_app.app_context():
        material_id = MaterialPrice.query.filter_by(key='qa_steel').first().id
    items = [{'material_id': material_id, 'quantity': 0}]
    worker_client.post('/storage/requests/create', data={'items_json': json.dumps(items)})
    with flask_app.app_context():
        assert MaterialRequest.query.count() == 0


def test_create_material_request_skips_invalid_lines_keeps_valid_ones(worker_client, app):
    with flask_app.app_context():
        material_id = MaterialPrice.query.filter_by(key='qa_steel').first().id
    items = [{'material_id': material_id, 'quantity': 3}, {'material_id': 999999, 'quantity': 1}]
    worker_client.post('/storage/requests/create', data={'items_json': json.dumps(items)})
    with flask_app.app_context():
        assert MaterialRequest.query.count() == 1
        assert MaterialRequest.query.first().material_id == material_id


def test_update_status_to_ordered_requires_supplier(worker_client, app):
    with flask_app.app_context():
        material_id = MaterialPrice.query.filter_by(key='qa_steel').first().id
        req = MaterialRequest(material_id=material_id, quantity=3)
        db.session.add(req)
        db.session.commit()
        req_id = req.id

    res = worker_client.post(f'/storage/requests/{req_id}/update', data={'status': 'ordered'})
    assert res.status_code == 302
    with flask_app.app_context():
        req = MaterialRequest.query.get(req_id)
        assert req.status == 'new'  # rejected, unchanged - no supplier given


def test_update_status_to_ordered_with_supplier(worker_client, app):
    with flask_app.app_context():
        material_id = MaterialPrice.query.filter_by(key='qa_steel').first().id
        supplier_id = Supplier.query.first().id
        req = MaterialRequest(material_id=material_id, quantity=3)
        db.session.add(req)
        db.session.commit()
        req_id = req.id

    worker_client.post(f'/storage/requests/{req_id}/update', data={'status': 'ordered', 'supplier_id': supplier_id})
    with flask_app.app_context():
        req = MaterialRequest.query.get(req_id)
        assert req.status == 'ordered'
        assert req.supplier_id == supplier_id


def test_update_status_back_to_processing_clears_supplier(worker_client, app):
    with flask_app.app_context():
        material_id = MaterialPrice.query.filter_by(key='qa_steel').first().id
        supplier_id = Supplier.query.first().id
        req = MaterialRequest(material_id=material_id, quantity=3, status='ordered', supplier_id=supplier_id)
        db.session.add(req)
        db.session.commit()
        req_id = req.id

    worker_client.post(f'/storage/requests/{req_id}/update', data={'status': 'processing'})
    with flask_app.app_context():
        req = MaterialRequest.query.get(req_id)
        assert req.status == 'processing'
        assert req.supplier_id is None
