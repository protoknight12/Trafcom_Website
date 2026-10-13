# -*- coding: utf-8 -*-
"""
pytest regression tests for the login/account hardening in app.py:

1. 2FA step: lockout after TOTP_MAX_FAILURES wrong codes, the pending login
   expires, a code is accepted once (no replay).
2. Password-reset token is bound to the current password hash - dead after
   its first use or any other password change; the old bare-id token is invalid.
3. Email-verify token carries the address - a link for an old address does
   not verify a newer one.
4. Changing the account email needs the current password.
5. PUBLIC_BASE_URL, when set, builds the mailed links (not the Host header).
6. Username rule at registration / admin user creation; passwords stripped
   at creation like login() strips them.
7. Failed logins are logged; security headers on every response; the
   immutable cache header only on a versioned static URL.

Run with:
    pytest testing/test_auth_hardening.py -v
"""
import atexit
import logging
import os
import re
import tempfile
import time

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
from flask import g, url_for
from werkzeug.security import generate_password_hash, check_password_hash

import app as app_module
from app import (app as flask_app, db, User, limiter, _reset_serializer, _reset_token, _verify_serializer,
                 _valid_username, TOTP_MAX_FAILURES, TOTP_FAILURE_WINDOW, TOTP_PENDING_MAX_AGE)


@pytest.fixture
def sent_emails(monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, 'send_email', lambda to, subject, body: sent.append((to, subject, body)) or True)
    return sent


@pytest.fixture
def app(sent_emails, monkeypatch):
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
    limiter.reset()
    # in-memory 2FA state is keyed by user id, and every test starts again at id 1
    app_module._totp_failures.clear()
    app_module._totp_last_step.clear()
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _make_user(username='user1', password='password123', **kwargs):
    user = User(username=username, password=generate_password_hash(password, method='scrypt'), **kwargs)
    db.session.add(user)
    db.session.commit()
    return user


def _logged_in(client):
    with client.session_transaction() as s:
        return '_user_id' in s


def _password_step(client, username='tfa', password='password123'):
    return client.post('/login', data={'username': username, 'password': password})


# ------------------------------------------------------------------- 2FA

def test_2fa_lockout_after_five_wrong_codes(client, app):
    secret = pyotp.random_base32()
    user_id = _make_user('tfa', totp_secret=secret).id

    assert _password_step(client).headers['Location'].endswith('/login/2fa')
    for _ in range(TOTP_MAX_FAILURES - 1):
        assert client.post('/login/2fa', data={'code': '000000'}).status_code == 200  # form again
    last = client.post('/login/2fa', data={'code': '000000'})
    assert last.status_code == 302 and last.headers['Location'].endswith('/login')

    # the pending login is gone - even the right code is no use now
    with client.session_transaction() as s:
        assert 'pending_2fa_user_id' not in s
    good = client.post('/login/2fa', data={'code': pyotp.TOTP(secret).now()})
    assert good.headers['Location'].endswith('/login')
    assert not _logged_in(client)

    # ...and the right password does not open a new one while the user is locked
    assert _password_step(client).headers['Location'].endswith('/login')
    with client.session_transaction() as s:
        assert 'pending_2fa_user_id' not in s

    # a pending login another browser already had is refused too
    with client.session_transaction() as s:
        s['pending_2fa_user_id'] = user_id
        s['pending_2fa_at'] = time.time()
    client.post('/login/2fa', data={'code': pyotp.TOTP(secret).now()})
    assert not _logged_in(client)

    # once the window has passed the user can log in again
    app_module._totp_failures[user_id] = [t - TOTP_FAILURE_WINDOW - 1 for t in app_module._totp_failures[user_id]]
    assert _password_step(client).headers['Location'].endswith('/login/2fa')
    client.post('/login/2fa', data={'code': pyotp.TOTP(secret).now()})
    assert _logged_in(client)


def test_2fa_four_wrong_codes_do_not_lock(client, app):
    secret = pyotp.random_base32()
    _make_user('tfa', totp_secret=secret)
    _password_step(client)
    for _ in range(TOTP_MAX_FAILURES - 1):
        client.post('/login/2fa', data={'code': '000000'})
    client.post('/login/2fa', data={'code': pyotp.TOTP(secret).now()})
    assert _logged_in(client)


def test_2fa_pending_login_expires(client, app):
    secret = pyotp.random_base32()
    _make_user('tfa', totp_secret=secret)
    _password_step(client)
    with client.session_transaction() as s:
        s['pending_2fa_at'] = time.time() - TOTP_PENDING_MAX_AGE - 1

    resp = client.post('/login/2fa', data={'code': pyotp.TOTP(secret).now()})
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/login')
    assert not _logged_in(client)
    with client.session_transaction() as s:
        assert 'pending_2fa_user_id' not in s


def test_2fa_pending_login_without_timestamp_is_expired(client, app):
    """A pending login from before the timestamp existed must not live forever."""
    secret = pyotp.random_base32()
    user_id = _make_user('tfa', totp_secret=secret).id
    with client.session_transaction() as s:
        s['pending_2fa_user_id'] = user_id
    client.post('/login/2fa', data={'code': pyotp.TOTP(secret).now()})
    assert not _logged_in(client)


def test_2fa_code_cannot_be_replayed(app):
    secret = pyotp.random_base32()
    _make_user('tfa', totp_secret=secret)
    totp = pyotp.TOTP(secret)
    code = totp.now()

    first = app.test_client()
    _password_step(first)
    first.post('/login/2fa', data={'code': code})
    assert _logged_in(first)

    # the same code, seen over the shoulder, typed in another browser. (The fixture's app context is shared by
    # every request of a test, and flask-login caches the user in g - drop it, or "second" counts as logged in.)
    g.pop('_login_user', None)
    second = app.test_client()
    _password_step(second)
    replay = second.post('/login/2fa', data={'code': code})
    assert replay.status_code == 200  # the form again, with "invalid code"
    assert not _logged_in(second)

    # the next code still works (the +1 step of the drift window)
    second.post('/login/2fa', data={'code': totp.at(time.time() + totp.interval)})
    assert _logged_in(second)


# ----------------------------------------------------------- password reset

def test_reset_token_dies_on_first_use(client, app):
    user = _make_user('resetme', 'oldpassword1', email='reset@example.com')
    token = _reset_token(user)

    client.post(f'/reset-password/{token}', data={'password': 'newpassword1', 'password_confirm': 'newpassword1'})
    db.session.refresh(user)
    assert check_password_hash(user.password, 'newpassword1')

    again = client.post(f'/reset-password/{token}', data={'password': 'attacker-pw1', 'password_confirm': 'attacker-pw1'})
    assert again.status_code == 302 and again.headers['Location'].endswith('/forgot-password')
    db.session.refresh(user)
    assert check_password_hash(user.password, 'newpassword1')


def test_reset_token_rejected_after_password_changed(client, app):
    user = _make_user('resetme', 'oldpassword1', email='reset@example.com')
    token = _reset_token(user)
    user.password = generate_password_hash('changed-elsewhere1', method='scrypt')
    db.session.commit()

    assert client.get(f'/reset-password/{token}').headers['Location'].endswith('/forgot-password')
    client.post(f'/reset-password/{token}', data={'password': 'attacker-pw1', 'password_confirm': 'attacker-pw1'})
    db.session.refresh(user)
    assert check_password_hash(user.password, 'changed-elsewhere1')


def test_old_format_reset_token_is_invalid_not_a_crash(client, app):
    user = _make_user('resetme', 'oldpassword1', email='reset@example.com')
    old_token = _reset_serializer().dumps(user.id)
    resp = client.post(f'/reset-password/{old_token}', data={'password': 'attacker-pw1', 'password_confirm': 'attacker-pw1'})
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/forgot-password')
    db.session.refresh(user)
    assert check_password_hash(user.password, 'oldpassword1')


def test_reset_strips_password_like_login_does(client, app):
    user = _make_user('resetme', 'oldpassword1', email='reset@example.com')
    client.post(f'/reset-password/{_reset_token(user)}', data={'password': ' spaced-pass1 ', 'password_confirm': ' spaced-pass1 '})
    client.post('/login', data={'username': 'resetme', 'password': ' spaced-pass1 '})
    assert _logged_in(client)


# ------------------------------------------------------- email verification

def _verify_token(body):
    return re.search(r'/verify-email/(\S+)', body).group(1)


def test_verify_link_for_old_address_does_not_verify_new_one(client, app, sent_emails):
    client.post('/register', data={'username': 'mover', 'password': 'password123', 'email': 'old@example.com'})
    user = User.query.filter_by(username='mover').one()
    old_token = _verify_token(sent_emails[0][2])

    client.post('/login', data={'username': 'mover', 'password': 'password123'})
    client.post('/account/email', data={'email': 'new@example.com', 'password': 'password123'})
    db.session.refresh(user)
    assert user.email == 'new@example.com' and user.email_verified is False

    client.get(f'/verify-email/{old_token}')
    db.session.refresh(user)
    assert user.email_verified is False

    client.get(f'/verify-email/{_verify_token(sent_emails[1][2])}')
    db.session.refresh(user)
    assert user.email_verified is True


def test_old_format_verify_token_is_invalid_not_a_crash(client, app):
    user = _make_user('legacy', email='legacy@example.com', email_verified=False)
    resp = client.get(f'/verify-email/{_verify_serializer().dumps(user.id)}')
    assert resp.status_code == 302
    db.session.refresh(user)
    assert user.email_verified is False


# -------------------------------------------------------------- email change

@pytest.mark.parametrize('form_password', [None, 'wrong-password'])
def test_email_change_refused_without_the_right_password(client, app, sent_emails, form_password):
    user = _make_user('owner', 'password123', email='owner@example.com', email_verified=True)
    client.post('/login', data={'username': 'owner', 'password': 'password123'})

    data = {'email': 'attacker@example.com'}
    if form_password:
        data['password'] = form_password
    resp = client.post('/account/email', data=data, follow_redirects=True)

    assert 'Грешна парола'.encode('utf-8') in resp.data
    db.session.refresh(user)
    assert user.email == 'owner@example.com' and user.email_verified is True
    assert sent_emails == []


def test_account_page_asks_for_the_password_in_the_email_form(client, app):
    _make_user('owner', 'password123', email='owner@example.com')
    client.post('/login', data={'username': 'owner', 'password': 'password123'})
    page = client.get('/account').get_data(as_text=True)
    form = page[page.index('action="/account/email"'):]
    assert 'name="password"' in form[:form.index('</form>')]


# ---------------------------------------------------------- PUBLIC_BASE_URL

def test_mailed_links_use_public_base_url_not_the_host_header(client, app, sent_emails, monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://trafcombg.com/')
    _make_user('mailme', email='mailme@example.com')

    client.post('/forgot-password', data={'email': 'mailme@example.com'}, headers={'Host': 'evil.example'})
    client.post('/register', data={'username': 'fresh', 'password': 'password123', 'email': 'fresh@example.com'},
                headers={'Host': 'evil.example'})

    reset_body, verify_body = sent_emails[0][2], sent_emails[1][2]
    assert 'https://trafcombg.com/reset-password/' in reset_body
    assert 'https://trafcombg.com/verify-email/' in verify_body
    assert 'evil.example' not in reset_body + verify_body


def test_mailed_links_fall_back_to_the_request_host(client, app, sent_emails):
    _make_user('mailme', email='mailme@example.com')
    client.post('/forgot-password', data={'email': 'mailme@example.com'})
    assert 'http://localhost/reset-password/' in sent_emails[0][2]


# ------------------------------------------------------- usernames/passwords

def test_username_rule():
    for bad in ['', None, 'ab', 'a' * 51, '=1+1', '+cmd', '@sum', '-dash', '.dot', '_under', 'has space',
                '<b>x</b>', 'semi;colon', 'new\nline']:
        assert not _valid_username(bad), bad
    for good in ['abc', 'a' * 50, 'ivan.petrov', 'user_1', 'cnc-operator', '3dprint', 'Иван', 'Десислав.Цветанов-2']:
        assert _valid_username(good), good


@pytest.mark.parametrize('username', ['=HYPERLINK("http://x")', 'ab'])
def test_register_rejects_bad_username(client, app, username):
    resp = client.post('/register', data={'username': username, 'password': 'password123', 'email': 'x@example.com'})
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/register')
    assert User.query.count() == 0


def test_register_accepts_cyrillic_username_and_strips_password(client, app):
    client.post('/register', data={'username': ' Иван.Петров ', 'password': ' password123 ', 'email': 'ivan@example.com'})
    assert User.query.filter_by(username='Иван.Петров').one().email == 'ivan@example.com'
    client.post('/login', data={'username': 'Иван.Петров', 'password': 'password123'})
    assert _logged_in(client)


def test_admin_create_user_applies_the_username_rule(client, app):
    _make_user('boss', 'password123', role='admin')
    client.post('/login', data={'username': 'boss', 'password': 'password123'})

    client.post('/admin/create_user', data={'username': '=1+1', 'password': 'password123', 'role': 'worker'})
    assert User.query.count() == 1

    client.post('/admin/create_user', data={'username': 'Петър', 'password': 'password123', 'role': 'worker'})
    assert User.query.filter_by(username='Петър').one().role == 'worker'


# ------------------------------------------------------------------ logging

def test_failed_login_and_failed_code_are_logged(client, app, caplog):
    _make_user('tfa', totp_secret=pyotp.random_base32())
    with caplog.at_level(logging.WARNING, logger=flask_app.logger.name):
        client.post('/login', data={'username': 'nobody', 'password': 'wrong'})
        _password_step(client)
        client.post('/login/2fa', data={'code': '000000'})
    assert "Failed login for 'nobody' from 127.0.0.1" in caplog.text
    assert "Failed 2FA code for 'tfa' from 127.0.0.1" in caplog.text


# ------------------------------------------------------------------ headers

def test_security_headers_on_a_normal_response(client):
    resp = client.get('/login')
    assert resp.headers['X-Content-Type-Options'] == 'nosniff'
    assert resp.headers['X-Frame-Options'] == 'SAMEORIGIN'
    assert resp.headers['Referrer-Policy'] == 'strict-origin-when-cross-origin'
    assert 'immutable' not in resp.headers.get('Cache-Control', '')
    assert 'Content-Security-Policy' not in resp.headers


def test_immutable_cache_only_on_a_versioned_static_url(client, app):
    with app.test_request_context():
        versioned = url_for('static', filename='css/style.css')
    assert '?v=' in versioned

    resp = client.get(versioned)
    assert resp.status_code == 200
    assert resp.headers['Cache-Control'] == 'public, max-age=31536000, immutable'
    assert resp.headers['X-Content-Type-Options'] == 'nosniff'

    plain = client.get('/static/css/style.css')
    assert plain.status_code == 200
    assert 'immutable' not in plain.headers.get('Cache-Control', '')

    missing = client.get('/static/css/no-such-file.css?v=1')
    assert missing.status_code == 404
    assert 'immutable' not in missing.headers.get('Cache-Control', '')
