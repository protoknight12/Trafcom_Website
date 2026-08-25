"""
pytest regression test for the email verification flow added around
User.email_verified: register() sends a verification link, verify_email()
consumes it, and account_update_email() resets verification on address
change. Mocks send_email() so no real SMTP call happens - same throwaway
sqlite pattern as test_quick_create_material.py.

Run with:
    pytest testing/test_email_verification.py -v
"""
import atexit
import os
import re
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

import app as app_module
from app import app as flask_app, db, User, limiter


@pytest.fixture
def sent_emails(monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, 'send_email', lambda to, subject, body: sent.append((to, subject, body)) or True)
    return sent


@pytest.fixture
def app(sent_emails):
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


def _extract_token(body):
    m = re.search(r'/verify-email/([^\s]+)', body)
    assert m, f'no verification link found in email body: {body!r}'
    return m.group(1)


def test_register_sends_unverified_and_link_verifies(app, sent_emails):
    client = app.test_client()
    client.post('/register', data={
        'username': 'newbie', 'password': 'irrelevant123', 'email': 'newbie@example.com',
    })

    user = User.query.filter_by(username='newbie').first()
    assert user is not None
    assert user.email_verified is False
    assert len(sent_emails) == 1
    assert sent_emails[0][0] == 'newbie@example.com'

    token = _extract_token(sent_emails[0][2])
    resp = client.get(f'/verify-email/{token}', follow_redirects=True)
    assert resp.status_code == 200

    db.session.refresh(user)
    assert user.email_verified is True


def test_verify_email_bad_token_does_not_verify(app):
    client = app.test_client()
    client.post('/register', data={
        'username': 'newbie2', 'password': 'irrelevant123', 'email': 'newbie2@example.com',
    })
    user = User.query.filter_by(username='newbie2').first()

    client.get('/verify-email/not-a-real-token', follow_redirects=True)

    db.session.refresh(user)
    assert user.email_verified is False


def test_changing_email_resets_verification_and_resends(app, sent_emails):
    client = app.test_client()
    client.post('/register', data={
        'username': 'changer', 'password': 'irrelevant123', 'email': 'changer@example.com',
    })
    user = User.query.filter_by(username='changer').first()

    token = _extract_token(sent_emails[0][2])
    client.get(f'/verify-email/{token}')
    db.session.refresh(user)
    assert user.email_verified is True

    client.post('/login', data={'username': 'changer', 'password': 'irrelevant123'})
    client.post('/account/email', data={'email': 'changer-new@example.com'})

    db.session.refresh(user)
    assert user.email == 'changer-new@example.com'
    assert user.email_verified is False
    assert len(sent_emails) == 2
    assert sent_emails[1][0] == 'changer-new@example.com'
