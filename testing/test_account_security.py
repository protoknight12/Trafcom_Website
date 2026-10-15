# -*- coding: utf-8 -*-
"""
pytest regression tests for the logged-in account routes in app.py:

1. POST /account/password - needs the current password, same rules as reset_password(), old reset links die.
2. POST /delete_account - needs the password, never deletes the last admin.
3. 2FA: disable needs the password; enabling goes through _totp_accept() (the code can't be replayed at login).
4. The admin routes can't delete or demote the last admin.

Run with:
    pytest testing/test_account_security.py -v
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
from flask import g
from werkzeug.security import generate_password_hash, check_password_hash

import app as app_module
from app import app as flask_app, db, User, limiter, _reset_serializer, _reset_token

OLD = 'oldpassword1'
NEW = 'newpassword2'


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
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


def _make_user(username, role='regular_user', password=OLD, **kwargs):
    user = User(username=username, password=generate_password_hash(password, method='scrypt'),
                role=role, **kwargs)
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, username, password=OLD):
    # The fixture's app context is shared by every request of a test and flask-login caches the user in g -
    # drop it, or a second client would count as logged in as the first user.
    g.pop('_login_user', None)
    return client.post('/login', data={'username': username, 'password': password})


def _logged_in(client):
    with client.session_transaction() as s:
        return '_user_id' in s


def _password_ok(username, password):
    db.session.expire_all()
    return check_password_hash(User.query.filter_by(username=username).one().password, password)


def _post_password(client, current=OLD, new=NEW, confirm=None):
    return client.post('/account/password', data={
        'current_password': current, 'password': new, 'password_confirm': new if confirm is None else confirm})


# ------------------------------------------------------------ password change

def test_password_change_succeeds_and_new_password_logs_in(client, app):
    _make_user('alice')
    _login(client, 'alice')
    resp = _post_password(client)
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/account')
    assert _logged_in(client)  # stays logged in
    assert _password_ok('alice', NEW) and not _password_ok('alice', OLD)

    fresh = app.test_client()
    _login(fresh, 'alice', OLD)
    assert not _logged_in(fresh)
    _login(fresh, 'alice', NEW)
    assert _logged_in(fresh)


@pytest.mark.parametrize('kwargs', [
    {'current': 'wrongpassword'},
    {'new': 'short7!', 'confirm': 'short7!'},
    {'confirm': 'somethingelse9'},
])
def test_password_change_rejected_changes_nothing(client, app, kwargs):
    _make_user('alice')
    _login(client, 'alice')
    resp = _post_password(client, **kwargs)
    assert resp.status_code == 302
    assert _password_ok('alice', OLD) and not _password_ok('alice', NEW)


def test_password_change_invalidates_outstanding_reset_token(client, app):
    user = _make_user('alice', email='alice@example.com')
    token = _reset_token(user)
    _reset_serializer().loads(token)  # valid before
    _login(client, 'alice')
    _post_password(client)

    other = app.test_client()
    g.pop('_login_user', None)
    resp = other.post(f'/reset-password/{token}', data={'password': 'attacker-pass1', 'password_confirm': 'attacker-pass1'})
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/forgot-password')
    assert _password_ok('alice', NEW)


# ------------------------------------------------------------- delete account

def test_delete_account_needs_password(client, app):
    _make_user('admin1', role='admin')
    _make_user('bob')
    _login(client, 'bob')
    for data in ({}, {'password': 'wrongpassword'}):
        assert client.post('/delete_account', data=data).headers['Location'].endswith('/account')
        assert User.query.filter_by(username='bob').first() is not None
    assert _logged_in(client)


def test_delete_account_refuses_last_admin(client, app):
    _make_user('admin1', role='admin')
    _login(client, 'admin1')
    resp = client.post('/delete_account', data={'password': OLD})
    assert resp.headers['Location'].endswith('/account')
    assert User.query.filter_by(username='admin1').first() is not None
    assert _logged_in(client)


def test_delete_account_works_for_second_admin_and_regular_user(client, app):
    _make_user('admin1', role='admin')
    _make_user('admin2', role='admin')
    _make_user('bob')
    _login(client, 'bob')
    assert client.post('/delete_account', data={'password': OLD}).headers['Location'].endswith('/register')
    assert User.query.filter_by(username='bob').first() is None
    assert not _logged_in(client)

    other = app.test_client()
    _login(other, 'admin2')
    assert other.post('/delete_account', data={'password': OLD}).headers['Location'].endswith('/register')
    assert User.query.filter_by(username='admin2').first() is None
    assert User.query.filter_by(username='admin1').first() is not None


def test_delete_account_with_orders_is_refused_not_a_500(client, app):
    from app import Order, GeneratorPreset
    _make_user('admin1', role='admin')
    bob = _make_user('bob')
    db.session.add(Order(user_id=bob.id, order_number='T-1', customer_name='Bob'))
    db.session.add(GeneratorPreset(user_id=bob.id, name='p', settings_json='{}'))
    db.session.commit()
    _login(client, 'bob')
    resp = client.post('/delete_account', data={'password': OLD})
    assert resp.headers['Location'].endswith('/account')   # refused with a flash; the order would block the DELETE
    assert User.query.filter_by(username='bob').first() is not None
    assert _logged_in(client)


# ------------------------------------------------------------------------ 2FA

def test_2fa_disable_needs_password(client, app):
    secret = pyotp.random_base32()
    _make_user('carol', totp_secret=secret)
    # log in the 2FA way: password step, then the code
    client.post('/login', data={'username': 'carol', 'password': OLD})
    client.post('/login/2fa', data={'code': pyotp.TOTP(secret).now()})
    assert _logged_in(client)

    for data in ({}, {'password': 'wrongpassword'}):
        client.post('/account/2fa/disable', data=data)
        db.session.expire_all()
        assert User.query.filter_by(username='carol').one().totp_secret == secret
    client.post('/account/2fa/disable', data={'password': f' {OLD} '})  # stripped like everywhere else
    db.session.expire_all()
    assert User.query.filter_by(username='carol').one().totp_secret is None


def test_2fa_setup_code_cannot_be_replayed_at_login(client, app):
    _make_user('dave')
    _login(client, 'dave')
    client.get('/account/2fa/setup')
    with client.session_transaction() as s:
        secret = s['pending_totp_secret']
    code = pyotp.TOTP(secret).now()

    bad = client.post('/account/2fa/setup', data={'code': '000000'})
    assert bad.headers['Location'].endswith('/account/2fa/setup')
    db.session.expire_all()
    assert User.query.filter_by(username='dave').one().totp_secret is None

    client.post('/account/2fa/setup', data={'code': code})
    db.session.expire_all()
    assert User.query.filter_by(username='dave').one().totp_secret == secret

    # the very code that enabled 2FA is refused at the login step
    other = app.test_client()
    _login(other, 'dave')
    other.post('/login/2fa', data={'code': code})
    assert not _logged_in(other)


# --------------------------------------------------------- last admin (admin routes)

def test_last_admin_cannot_be_deleted_or_demoted_through_admin_routes(client, app):
    admin = _make_user('admin1', role='admin')
    _make_user('worker1', role='worker')
    _login(client, 'admin1')

    # the only admin cannot target themself ...
    client.post(f'/admin/delete_user/{admin.id}')
    client.post(f'/admin/update_role/{admin.id}', data={'role': 'regular_user'})
    client.post(f'/admin/users/{admin.id}/update', data={'role': 'regular_user'})
    db.session.expire_all()
    assert User.query.filter_by(username='admin1').one().role == 'admin'

    # ... and no non-admin can reach these routes to do it for them
    other = app.test_client()
    _login(other, 'worker1')
    other.post(f'/admin/delete_user/{admin.id}')
    other.post(f'/admin/update_role/{admin.id}', data={'role': 'worker'})
    other.post(f'/admin/users/{admin.id}/update', data={'role': 'worker'})
    db.session.expire_all()
    assert User.query.filter_by(username='admin1').one().role == 'admin'
