"""
pytest regression tests for the three new auth features: email on
registration/account, the password-reset token flow, and TOTP 2FA's
pending-login gate. Uses the Flask test client (same pattern as
test_security_fixes.py) since these need real request/session behavior.

Run with:
    pytest testing/test_auth_email_2fa.py -v
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

import pyotp
import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, limiter, _reset_serializer, PASSWORD_RESET_MAX_AGE


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


# ------------------------------------------------------------- registration

def test_register_requires_email(client, app):
    resp = client.post('/register', data={'username': 'noemail', 'password': 'longenough1'})
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/register')
    with app.app_context():
        assert User.query.filter_by(username='noemail').first() is None


def test_register_rejects_duplicate_email(client, app):
    client.post('/register', data={'username': 'user1', 'password': 'longenough1', 'email': 'dup@example.com'})
    resp = client.post('/register', data={'username': 'user2', 'password': 'longenough1', 'email': 'dup@example.com'})
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/register')
    with app.app_context():
        assert User.query.filter_by(username='user2').first() is None


# ----------------------------------------------------------- password reset

def test_forgot_password_unknown_email_gives_generic_message(client):
    """Must not reveal whether the address is registered (no enumeration)."""
    resp = client.post('/forgot-password', data={'email': 'nobody@example.com'}, follow_redirects=True)
    assert resp.status_code == 200
    assert 'изпратихме връзка'.encode('utf-8') in resp.data


def test_reset_password_token_roundtrip(client, app):
    with app.app_context():
        user = User(username='resetuser', email='reset@example.com',
                     password=generate_password_hash('oldpassword1', method='scrypt'))
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    with app.app_context():
        token = _reset_serializer().dumps(user_id)

    resp = client.post(f'/reset-password/{token}', data={'password': 'newpassword1'}, follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        refreshed = db.session.get(User, user_id)
        from werkzeug.security import check_password_hash
        assert check_password_hash(refreshed.password, 'newpassword1')
        assert not check_password_hash(refreshed.password, 'oldpassword1')


def test_reset_password_rejects_tampered_token(client, app):
    with app.app_context():
        user = User(username='tampuser', email='tamp@example.com',
                     password=generate_password_hash('oldpassword1', method='scrypt'))
        db.session.add(user)
        db.session.commit()

    resp = client.post('/reset-password/not-a-real-token', data={'password': 'newpassword1'}, follow_redirects=True)
    assert resp.status_code == 200
    assert resp.request.path == '/forgot-password'


# ------------------------------------------------------------------- 2FA

def test_login_with_2fa_requires_code(client, app):
    secret = pyotp.random_base32()
    with app.app_context():
        user = User(username='tfauser', password=generate_password_hash('password123', method='scrypt'),
                     totp_secret=secret)
        db.session.add(user)
        db.session.commit()

    # Correct password alone must NOT log the user in yet.
    resp = client.post('/login', data={'username': 'tfauser', 'password': 'password123'}, follow_redirects=True)
    assert resp.request.path == '/login/2fa'

    dashboard = client.get('/dashboard')
    assert dashboard.status_code in (302, 401, 403)  # not authenticated yet

    # Wrong code doesn't log in.
    bad = client.post('/login/2fa', data={'code': '000000'}, follow_redirects=True)
    assert bad.request.path == '/login/2fa'

    # Correct TOTP code completes login.
    good_code = pyotp.TOTP(secret).now()
    good = client.post('/login/2fa', data={'code': good_code}, follow_redirects=True)
    assert good.request.path != '/login/2fa'


def test_login_without_2fa_secret_skips_gate(client, app):
    with app.app_context():
        user = User(username='plainuser', password=generate_password_hash('password123', method='scrypt'))
        db.session.add(user)
        db.session.commit()

    resp = client.post('/login', data={'username': 'plainuser', 'password': 'password123'}, follow_redirects=True)
    assert resp.request.path != '/login/2fa'
