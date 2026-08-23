"""
pytest regression test for the registration lock (registration_closed() /
admin_toggle_registration() in app.py, checkbox on admin_users.html): when an
admin flips it on, POST /register must refuse new signups with a maintenance
flash, while an already-existing user can still log in. Uses the Flask test
client (same pattern as test_quick_create_material.py) since this needs real
request/session/redirect behavior.

Run with:
    pytest testing/test_registration_lock.py -v
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

from app import app as flask_app, db, User, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        existing = User(username='qa_existing', password=generate_password_hash('irrelevant123'), role='regular_user')
        db.session.add_all([admin, existing])
        db.session.commit()
    # No app context held across the yield: this test exercises two
    # differently-authenticated test clients (admin_client and an anonymous
    # one) in the same test, and an ambient app context would make Flask
    # reuse one shared `flask.g` for every request instead of a fresh one
    # per request - Flask-Login caches the loaded user on `g`, so the
    # "anonymous" client would inherit the admin client's cached identity.
    yield flask_app
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def admin_client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c


def test_toggle_blocks_new_registration_but_not_existing_login(app, admin_client):
    resp = admin_client.post('/admin/users/toggle-registration', data={'registration_closed': '1'}, follow_redirects=True)
    assert resp.status_code == 200

    anon = app.test_client()
    resp = anon.post('/register', data={'username': 'new_guy', 'password': 'longenough1'}, follow_redirects=True)
    assert resp.status_code == 200
    assert resp.request.path == '/login'
    assert 'поддръжка'.encode('utf-8') in resp.data
    with app.app_context():
        assert User.query.filter_by(username='new_guy').first() is None

    resp = anon.post('/login', data={'username': 'qa_existing', 'password': 'irrelevant123'}, follow_redirects=True)
    assert resp.status_code == 200
    assert b'/logout' in resp.data or b'dashboard' in resp.data.lower()


def test_toggle_off_allows_registration_again(app, admin_client):
    admin_client.post('/admin/users/toggle-registration', data={'registration_closed': '1'})
    admin_client.post('/admin/users/toggle-registration', data={'registration_closed': '0'})

    anon = app.test_client()
    resp = anon.post('/register', data={'username': 'new_guy2', 'password': 'longenough1', 'email': 'new_guy2@example.com'}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert User.query.filter_by(username='new_guy2').first() is not None
