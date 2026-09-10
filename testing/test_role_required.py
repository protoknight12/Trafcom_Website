"""
pytest regression test for role_required() itself (see app.py) - the single
decorator gating all ~186 admin-only routes across the app. Existing
route-level tests (test_activity_log.py, test_generator_presets.py,
test_shelly_device_routes.py) only exercise it incidentally through 5
specific business routes; this test hits the decorator directly via two
throwaway routes registered on the same Flask app, so a regression in
role_required()'s own branching (string vs. list roles, the flash message,
the redirect target) is caught regardless of which business route someone
happens to touch next.

Run with:
    pytest testing/test_role_required.py -v
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

from app import app as flask_app, db, User, role_required, limiter

flask_app.add_url_rule('/_qa/admin-only', '_qa_admin_only',
                        role_required('admin')(lambda: 'ok'))
flask_app.add_url_rule('/_qa/admin-or-worker', '_qa_admin_or_worker',
                        role_required(['admin', 'worker'])(lambda: 'ok'))


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add_all([
            User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'),
            User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker'),
            User(username='qa_regular', password=generate_password_hash('irrelevant123'), role='regular_user'),
        ])
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


def _login(app, username):
    c = app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


@pytest.fixture
def admin_client(app):
    return _login(app, 'qa_admin')


@pytest.fixture
def worker_client(app):
    return _login(app, 'qa_worker')


@pytest.fixture
def regular_client(app):
    return _login(app, 'qa_regular')


def test_anonymous_is_sent_to_login(app):
    res = app.test_client().get('/_qa/admin-only')
    assert res.status_code == 302
    assert '/login' in res.headers['Location']


def test_disallowed_role_is_redirected_to_dashboard_with_flash(regular_client):
    res = regular_client.get('/_qa/admin-only')
    assert res.status_code == 302
    assert res.headers['Location'].endswith('/dashboard')

    res = regular_client.get('/_qa/admin-only', follow_redirects=True)
    assert 'Нямате разрешение' in res.get_data(as_text=True)


def test_allowed_role_string_form_passes_through(admin_client):
    res = admin_client.get('/_qa/admin-only')
    assert res.status_code == 200
    assert res.get_data(as_text=True) == 'ok'


def test_allowed_role_list_form_passes_through_for_either_role(admin_client, worker_client):
    assert admin_client.get('/_qa/admin-or-worker').status_code == 200
    assert worker_client.get('/_qa/admin-or-worker').status_code == 200


def test_disallowed_role_list_form_is_rejected(regular_client):
    res = regular_client.get('/_qa/admin-or-worker')
    assert res.status_code == 302
    assert res.headers['Location'].endswith('/dashboard')
