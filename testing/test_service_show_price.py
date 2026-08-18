"""
pytest regression test for Service.show_price - the /admin/services checkbox
that controls whether a service's hourly rate appears on the public
/services page. Uses the Flask test client (same pattern as
test_security_fixes.py) since this needs real request/session/login behavior
and template rendering.

Run with:
    pytest testing/test_service_show_price.py -v
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

from app import app as flask_app, db, User, Service, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        db.session.add(admin)
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def admin_client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c


def _add_service(client, name, show_price):
    data = {'name': name, 'price_per_hour_eur': '50.00', 'pricing_mode': 'time'}
    if show_price:
        data['show_price'] = 'on'
    return client.post('/admin/services/add', data=data)


def test_new_service_defaults_to_price_shown(admin_client, app):
    _add_service(admin_client, 'QA Рязане', show_price=True)
    with app.app_context():
        assert Service.query.filter_by(name='QA Рязане').first().show_price is True

    html = admin_client.get('/services').get_data(as_text=True)
    assert 'QA Рязане' in html
    assert '50.00 €' in html


def test_unchecking_show_price_hides_price_on_public_page(admin_client, app):
    _add_service(admin_client, 'QA Огъване', show_price=False)
    with app.app_context():
        service = Service.query.filter_by(name='QA Огъване').first()
        assert service.show_price is False

    html = admin_client.get('/services').get_data(as_text=True)
    assert 'QA Огъване' in html  # service card itself still lists
    assert '50.00 €' not in html  # but its price is not rendered


def test_update_service_toggles_show_price_off(admin_client, app):
    _add_service(admin_client, 'QA Заваряване', show_price=True)
    with app.app_context():
        service = Service.query.filter_by(name='QA Заваряване').first()
        service_id = service.id

    admin_client.post(f'/admin/services/{service_id}/update', data={
        'name': 'QA Заваряване', 'price_per_hour_eur': '50.00', 'pricing_mode': 'time',
        # show_price omitted - an unchecked checkbox
    })

    with app.app_context():
        assert Service.query.get(service_id).show_price is False
