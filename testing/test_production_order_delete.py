"""
pytest regression test for delete_production_order()'s soft-delete rule: a
'pending' job never touched stock, so deleting it is a hard delete. A 'done'
job DID move real stock (see complete_production_order()), so deleting it
must be a SOFT delete instead - reversed_at/reversed_by_id set, row kept -
so admin_material_history() can still show both the original "taken for
production" movement and this reversing "returned from production" movement.
Also covers that admin_production_orders()'s done_jobs list excludes a job
once it's been reversed (it's no longer active). Uses the Flask test client
(same pattern as test_production_order_clamp.py) since this needs real
request/redirect/DB-state behavior.

Run with:
    pytest testing/test_production_order_delete.py -v
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
        material = MaterialPrice(key='qa_sheet', display_name='QA Ламарина', cost_per_m2=10.0,
                                  cutting_speed_mm_per_min=1000, pierce_rate_per_min=30, type='sheets',
                                  stock_quantity=5.0)
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


def test_deleting_a_pending_job_hard_deletes(app, worker_client):
    detail_id, material_id = _ids(app)
    worker_client.post('/admin/production-orders/create',
                        data={'detail_id': detail_id, 'material_id': material_id, 'quantity': 2})
    with flask_app.app_context():
        job_id = ProductionOrder.query.one().id

    worker_client.post(f'/admin/production-orders/{job_id}/delete', follow_redirects=True)
    with flask_app.app_context():
        assert ProductionOrder.query.count() == 0, "a pending job never touched stock - nothing to keep a record of"


def test_deleting_a_done_job_soft_deletes_and_appears_in_history(app, worker_client):
    detail_id, material_id = _ids(app)
    worker_client.post('/admin/production-orders/create',
                        data={'detail_id': detail_id, 'material_id': material_id, 'quantity': 2})
    with flask_app.app_context():
        job_id = ProductionOrder.query.one().id
    worker_client.post(f'/admin/production-orders/{job_id}/complete', data={'actual_material_qty': '1.1'})

    worker_client.post(f'/admin/production-orders/{job_id}/delete', follow_redirects=True)

    with flask_app.app_context():
        job = ProductionOrder.query.get(job_id)
        assert job is not None, "a completed job must be kept (soft-deleted), not removed"
        assert job.status == 'done'
        assert job.reversed_at is not None
        assert job.reversed_by_id is not None

    # No longer shown as an active job on the wizard page (the only job in
    # this fixture DB was just reversed, so the jobs table falls back to its
    # empty-state row - "QA Detail" still legitimately appears elsewhere on
    # the page, in the unrelated "pick a detail" dropdown).
    res = worker_client.get('/admin/production-orders')
    assert res.status_code == 200
    assert 'Няма задачи за производство' in res.get_data(as_text=True)

    # Both the original take and the reversing return show up in this
    # material's history, in the material's own stock unit (m^2 here, no
    # sheet dims recorded on this fixture material - see _material_stock_delta).
    hist = worker_client.get(f'/admin/materials/{material_id}/history')
    assert hist.status_code == 200
    body = hist.get_data(as_text=True)
    assert 'Взето за производство' in body
    assert 'Върнато от производство' in body


def test_deleting_an_already_reversed_job_is_refused(app, worker_client):
    detail_id, material_id = _ids(app)
    worker_client.post('/admin/production-orders/create',
                        data={'detail_id': detail_id, 'material_id': material_id, 'quantity': 2})
    with flask_app.app_context():
        job_id = ProductionOrder.query.one().id
    worker_client.post(f'/admin/production-orders/{job_id}/complete', data={'actual_material_qty': '1.0'})
    worker_client.post(f'/admin/production-orders/{job_id}/delete')

    with flask_app.app_context():
        m = MaterialPrice.query.filter_by(key='qa_sheet').first()
        stock_after_first_delete = m.stock_quantity

    # A second delete attempt on the same (already-reversed) job must be a
    # no-op, not double-reverse the stock.
    worker_client.post(f'/admin/production-orders/{job_id}/delete')
    with flask_app.app_context():
        m = MaterialPrice.query.filter_by(key='qa_sheet').first()
        assert m.stock_quantity == stock_after_first_delete
