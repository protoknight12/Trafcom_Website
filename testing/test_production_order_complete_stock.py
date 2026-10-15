"""
pytest: complete_production_order() must never drive a material batch below
zero - pending jobs reserve nothing, so the check is repeated at completion
(lot locked, refused with a flash, job stays pending, stock untouched).

Run with:
    pytest testing/test_production_order_complete_stock.py -v
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
        pass  # Windows keeps the file locked while SQLAlchemy's pooled connection is open


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
        db.session.add(User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker'))
        # No sheet size on record: stock_quantity is already m^2. 1000x500 mm piece = 0.5 m^2.
        material = MaterialPrice(key='qa_sheet', display_name='QA Ламарина', cost_per_m2=10.0,
                                 cutting_speed_mm_per_min=1000, pierce_rate_per_min=30, type='sheets',
                                 stock_quantity=1.0)
        db.session.add(material)
        db.session.flush()
        db.session.add(Detail(name='QA Detail', material_key=material.key, width=1000.0, height=500.0,
                              total_length=0.0, pierce_count=0, calculated_price=5.0))
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_worker', 'password': 'irrelevant123'})
    return c


def _job(qty=1, planned=0.5):
    """A pending job straight in the DB (as if it fitted when it was created); returns its id."""
    with flask_app.app_context():
        d = Detail.query.filter_by(name='QA Detail').first()
        m = MaterialPrice.query.filter_by(key='qa_sheet').first()
        job = ProductionOrder(detail_id=d.id, material_id=m.id, quantity=qty, planned_material_qty=planned)
        db.session.add(job)
        db.session.commit()
        return job.id


def _complete(client, job_id, actual):
    return client.post(f'/admin/production-orders/{job_id}/complete',
                       data={'actual_material_qty': actual}, follow_redirects=True)


def _state(job_id):
    with flask_app.app_context():
        job = db.session.get(ProductionOrder, job_id)
        return (job.status, MaterialPrice.query.filter_by(key='qa_sheet').first().stock_quantity,
                Detail.query.filter_by(name='QA Detail').first().stock_quantity or 0)


def test_enough_stock_completes_as_before(client):
    jid = _job()
    res = _complete(client, jid, 0.5)
    assert 'наличностите са обновени' in res.get_data(as_text=True)
    assert _state(jid) == ('done', 0.5, 1)


def test_second_job_refused_after_first_consumed_the_stock(client):
    j1, j2 = _job(planned=0.75), _job(planned=0.75)  # each fitted alone when created
    _complete(client, j1, 0.75)
    res = _complete(client, j2, 0.75)
    body = res.get_data(as_text=True)
    assert 'не може да се завърши' in body and 'QA Ламарина' in body and 'Запишете доставка' in body
    status, stock, detail_stock = _state(j2)
    assert status == 'pending'
    assert stock == 0.25 and detail_stock == 1, "refused completion must move no stock"


def test_actual_above_available_is_refused(client):
    jid = _job()
    res = _complete(client, jid, 1.5)
    assert 'не може да се завърши' in res.get_data(as_text=True)
    assert _state(jid) == ('pending', 1.0, 0)


def test_exact_equal_stock_to_zero_is_allowed(client):
    jid = _job()
    _complete(client, jid, 1.0)
    assert _state(jid) == ('done', 0.0, 1)


def test_float_noise_does_not_refuse(client):
    with flask_app.app_context():
        MaterialPrice.query.filter_by(key='qa_sheet').first().stock_quantity = 0.3
        db.session.commit()
    jid = _job()
    _complete(client, jid, 0.1 + 0.1 + 0.1)  # 0.30000000000000004
    assert _state(jid)[0] == 'done'
