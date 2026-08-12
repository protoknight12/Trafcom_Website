"""
pytest regression test: detail_dxf_dashboard.html's operations price
breakdown must exclude extra cutting-service (machine_type == 'laser')
Operations from the "operations total" row - cutting gets its own separate
row instead, since it's "a separate, dedicated operation" - while the grand
total (material + all operations) still includes everything. Uses the Flask
test client (same pattern as test_operation_description.py) since this needs
real request/session/login behavior.

Run with:
    pytest testing/test_operations_breakdown_excludes_cutting.py -v
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

from app import app as flask_app, db, User, MaterialPrice, Detail, Service, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        material = MaterialPrice(key='qa_mat', display_name='QA Material', cost_per_m2=10,
                                  cutting_speed_mm_per_min=1, pierce_rate_per_min=0.1)
        # An extra laser-cutting service (machine_type='laser'), same category as
        # the base cut - see Detail.cutting_service / calculate_cnc_price.
        laser = Service(name='Допълнително лазерно рязане', machine_type='laser', price_per_hour_eur=60.0)
        mill = Service(name='Фрезоване', machine_type='mill_3axis', price_per_hour_eur=60.0)
        db.session.add_all([admin, material, laser, mill])
        db.session.flush()
        detail = Detail(name='QA Detail', material_key=material.key, width=10, height=10,
                         total_length=1, pierce_count=1, calculated_price=20.0)
        db.session.add(detail)
        db.session.commit()
        yield flask_app, detail.id, laser.id, mill.id
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    flask_app, detail_id, laser_id, mill_id = app
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c, detail_id, laser_id, mill_id


def test_cutting_operation_excluded_from_operations_total_but_counted_in_grand_total(client):
    c, detail_id, laser_id, mill_id = client
    rows = [
        {'service_id': laser_id, 'duration_minutes': 30},  # 30 EUR, cutting -> own row
        {'service_id': mill_id, 'duration_minutes': 15},   # 15 EUR, non-cutting -> operations total
    ]
    c.post(f'/admin/details/{detail_id}/operations/add', data={'operations_json': json.dumps(rows)})
    res = c.get(f'/details/{detail_id}/files')
    assert res.status_code == 200
    html = res.data.decode()

    assert '15.00' in html  # operations-total row: only the mill op, not the laser one
    assert '30.00' in html  # cutting row: only the laser op
    assert '65.00' in html  # grand total: 20 base + 30 cutting + 15 milling
