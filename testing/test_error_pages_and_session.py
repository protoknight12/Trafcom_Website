"""
pytest regression tests for the custom error pages and the idle-timeout
session (login sets session.permanent, cookie expiry is bounded by
PERMANENT_SESSION_LIFETIME). Same throwaway-SQLite pattern as
test_security_fixes.py - see that file's docstring for why pytest instead of
a plain assert script.

Run with: pytest testing/test_error_pages_and_session.py -v
"""
import atexit
import os
import tempfile
from datetime import timedelta

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)


def _cleanup_db_file():
    try:
        os.remove(_db_path)
    except OSError:
        pass


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
        db.session.add(User(username='alice', password=generate_password_hash('pw1234567'), role='regular_user'))
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def test_404_renders_custom_error_page(client):
    resp = client.get('/this-route-does-not-exist')
    assert resp.status_code == 404
    assert 'Страницата не е намерена'.encode('utf-8') in resp.data


def test_session_lifetime_is_configured():
    assert flask_app.config['PERMANENT_SESSION_LIFETIME'] == timedelta(hours=10)


def test_login_marks_session_permanent(client):
    resp = client.post('/login', data={'username': 'alice', 'password': 'pw1234567'})
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess.permanent is True
