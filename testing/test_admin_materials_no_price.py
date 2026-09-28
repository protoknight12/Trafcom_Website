"""
pytest: admin_materials() no longer lets an admin set a material price -
price only ever comes from a delivery note (see
_find_or_create_delivery_target). Covers add (posted price ignored, starts
at 0), edit via the modal form (rename works, price untouched), and the
read-only table rendering the "няма доставна цена" warning + edit button.

Run with:
    pytest testing/test_admin_materials_no_price.py -v
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

from app import app as flask_app, db, User, MaterialPrice, limiter


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def test_add_ignores_price_and_edit_keeps_it(admin_client):
    admin_client.post('/admin/add_material', data={
        'display_name': 'QA Лист', 'type': 'sheets', 'cost_per_m2': '99', 'price_per_unit': '5',
        'cutting_speed_mm_per_min': '1500', 'pierce_time_sec': '2', 'thickness_mm': '2',
    })
    m = MaterialPrice.query.filter_by(display_name='QA Лист').one()
    assert m.cost_per_m2 == 0 and m.price_per_unit is None

    page = admin_client.get('/admin/materials').get_data(as_text=True)
    assert 'няма доставна цена' in page
    assert 'Редактирай' in page and 'id="materialDialog"' in page
    assert 'name="cost_per_m2"' not in page

    m.cost_per_m2 = 12.5  # as if a delivery note priced it
    db.session.commit()
    admin_client.post(f'/admin/update_material/{m.key}', data={
        'display_name': 'QA Лист нов', 'type': 'sheets', 'cost_per_m2': '1',
        'cutting_speed_mm_per_min': '1600', 'pierce_time_sec': '2',
    })
    db.session.refresh(m)
    assert m.display_name == 'QA Лист нов'
    assert m.cutting_speed_mm_per_min == 1600
    assert m.cost_per_m2 == 12.5, 'edit must never touch the delivery-derived price'
