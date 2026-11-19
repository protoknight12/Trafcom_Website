"""
pytest: purchase prices / quantities / kg show 3 decimals on screen and 2 on a print (owner's decision 2026-11-19).

Run with:
    pytest testing/test_number_precision.py -q
"""
import atexit
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)
atexit.register(lambda: os.path.exists(_db_path) and _try_remove())


def _try_remove():
    try:
        os.remove(_db_path)
    except OSError:
        pass


os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, MaterialPrice, limiter, num3, num2, price3, kg_text


def test_filters():
    assert num3(1.25) == '1.25' and num3(0.9421) == '0.942' and num3(12) == '12' and num3(None) == ''
    assert num2(0.9426) == '0.94' and num2(12.0) == '12'
    assert price3(14.5) == '14.50' and price3(14.505) == '14.505' and price3(None) == ''
    assert kg_text(12.3456) == ' (≈ 12.346 кг)' and kg_text(12.3456, 2) == ' (≈ 12.35 кг)' and kg_text(0) == ''


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.add(MaterialPrice(key='prec1', display_name='QA Точност', type='sheets', cost_per_m2=14.505,
                                     stock_quantity=1.2345, sheet_length_mm=3000, sheet_width_mm=1500,
                                     thickness_mm=2, cutting_speed_mm_per_min=1500, pierce_rate_per_min=30))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def test_screen_shows_three_decimals(admin_client):
    page = admin_client.get('/admin/materials').get_data(as_text=True)
    assert '14.505' in page and '14.51' not in page
    assert '1.234' in page or '1.235' in page


def test_print_filters_round_to_two():
    out = flask_app.jinja_env.from_string("{{ 2.3456|num2 }}|{{ 2.3456|num3 }}|{{ 1.2345|kg(2) }}").render()
    assert out == '2.35|2.346| (≈ 1.23 кг)'
