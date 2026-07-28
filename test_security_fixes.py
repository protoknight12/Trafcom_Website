"""
pytest regression tests for the four audit fixes (stored XSS via DXF filename,
missing CSRF protection, weak registration password policy, unvalidated
machine status input). Unlike the rest of this repo's test_*.py scripts
(plain assert, run directly), these use pytest + Flask's test client because
verifying request/response behavior (redirects, status codes, rendered HTML,
session auth) needs a real app context - not something a standalone assert
script can drive cleanly.

Run with:
    pip install pytest flask-wtf
    pytest test_security_fixes.py -v

Uses a throwaway file-based SQLite DB (not the real Postgres DATABASE_URL)
so tests run in isolation and never touch a real database.
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

# app.py reads these from the environment at import time and raises
# RuntimeError if either is missing - must be set before `import app`.
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Machine, DxfFile, limiter, sanitize_display_filename


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()  # tests share one process-wide limiter; don't let call order trip 429s
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _register(client, username, password):
    return client.post('/register', data={'username': username, 'password': password})


def _login(client, username, password):
    return client.post('/login', data={'username': username, 'password': password})


# ---------------------------------------------------------------- Vuln 1: XSS

def test_sanitize_display_filename_strips_dangerous_chars():
    payload = "x');alert(document.cookie);//.dxf"
    cleaned = sanitize_display_filename(payload)
    assert "'" not in cleaned
    assert '"' not in cleaned
    assert '<' not in cleaned and '>' not in cleaned
    # Everything else (including the rest of a Cyrillic filename) is kept.
    assert cleaned == 'x);alert(document.cookie);//.dxf'
    bulgarian = sanitize_display_filename('Чертеж_2026.dxf')
    assert bulgarian == 'Чертеж_2026.dxf'


def test_dxf_filename_xss_mitigation(client, app):
    with app.app_context():
        user = User(username='xssuser', password=generate_password_hash('longpassword1', method='scrypt'))
        db.session.add(user)
        db.session.commit()
        user_id = user.id

        # Insert directly (bypassing the upload pipeline) with an already-
        # dangerous filename, so this test exercises the template's own
        # defense independent of the storage-layer sanitizer above.
        db.session.add(DxfFile(
            filename="x');alert(document.cookie);//.dxf", material='steel',
            width=10, height=10, total_length=10, calculated_price=5.0,
            user_id=user_id,
        ))
        db.session.commit()

    _login(client, 'xssuser', 'longpassword1')
    html = client.get('/dashboard').get_data(as_text=True)

    # The old vulnerable pattern - filename string-concatenated straight
    # into an inline onclick handler - must be gone.
    assert 'openDxfViewer(' not in html or "', '" not in html.split('openDxfViewer(', 1)[1].split(')', 1)[0]
    assert "onclick=\"openDxfViewer(" not in html
    # The filename must instead travel through a data-* attribute, which
    # JS reads as a plain string (via .dataset) and never re-parses as code.
    assert 'data-filename="' in html


# --------------------------------------------------------------- Vuln 2: CSRF

def test_csrf_protection_enforcement(client, app):
    # Set up the account with CSRF checks off (this fixture's default) -
    # the test is about the *absence* of a token on the target request,
    # not about registration/login themselves.
    _register(client, 'csrfuser', 'longpassword1')
    _login(client, 'csrfuser', 'longpassword1')

    # Now turn CSRFProtect's enforcement on and fire a real state-changing
    # POST with no csrf_token field/header at all.
    app.config['WTF_CSRF_ENABLED'] = True
    resp = client.post('/delete_account')
    assert resp.status_code == 400

    with app.app_context():
        assert User.query.filter_by(username='csrfuser').first() is not None


# ---------------------------------------------------------- Vuln 3: password

def test_password_length_validation(client, app):
    resp = _register(client, 'shortpwuser', '1234567')  # 7 chars, one short of the floor
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/register')

    with app.app_context():
        assert User.query.filter_by(username='shortpwuser').first() is None

    html = client.get(resp.headers['Location']).get_data(as_text=True)
    assert 'поне 8 символа' in html


def test_password_length_validation_accepts_valid_length(client, app):
    resp = _register(client, 'okpwuser', 'longenough1')  # 11 chars, passes
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/login')

    with app.app_context():
        assert User.query.filter_by(username='okpwuser').first() is not None


# ---------------------------------------------------------- Vuln 4: whitelist

def test_machine_status_whitelisting(client, app):
    with app.app_context():
        worker = User(username='worker1', password=generate_password_hash('longpassword1', method='scrypt'), role='worker')
        machine = Machine(name='Test Laser', status='idle')
        db.session.add_all([worker, machine])
        db.session.commit()
        machine_id = machine.id

    _login(client, 'worker1', 'longpassword1')

    resp = client.post(f'/machines/update/{machine_id}', data={'status': 'HACKED'})
    assert resp.status_code == 400

    with app.app_context():
        refreshed = db.session.get(Machine, machine_id)
        assert refreshed.status == 'idle'  # unchanged


def test_machine_status_whitelisting_accepts_valid_value(client, app):
    with app.app_context():
        worker = User(username='worker2', password=generate_password_hash('longpassword1', method='scrypt'), role='worker')
        machine = Machine(name='Test Laser 2', status='idle')
        db.session.add_all([worker, machine])
        db.session.commit()
        machine_id = machine.id

    _login(client, 'worker2', 'longpassword1')

    resp = client.post(f'/machines/update/{machine_id}', data={'status': 'running'})
    assert resp.status_code == 302

    with app.app_context():
        refreshed = db.session.get(Machine, machine_id)
        assert refreshed.status == 'running'
