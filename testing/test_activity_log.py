"""
pytest regression test for the ActivityLog audit trail (see ActivityLog model
and the _log_activity() after_request hook in app.py, plus admin_log() /
admin_log_clear() / admin_log_export()).

Guards: a state-changing (POST) request by a logged-in user creates a row;
a GET request does not (would spam the log with page views/polling); a
non-admin can't reach the log page or clear it; admin_log_clear() wipes the
table (but is itself logged, since the hook runs after the clear commits).

Run with:
    pytest testing/test_activity_log.py -v
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

from app import app as flask_app, db, User, ActivityLog, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        regular = User(username='qa_regular', password=generate_password_hash('irrelevant123'), role='regular_user')
        db.session.add_all([admin, regular])
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def admin_client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c


@pytest.fixture
def regular_client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_regular', 'password': 'irrelevant123'})
    return c


def test_get_request_not_logged(app, admin_client):
    with app.app_context():
        before = ActivityLog.query.count()
    admin_client.get('/admin/log')
    with app.app_context():
        assert ActivityLog.query.count() == before


def test_post_request_is_logged(app, regular_client):
    # login_required (not role-gated) POST route; the view itself denies
    # non-admins internally, but that's still a real POST worth auditing -
    # the hook logs it regardless of what the view decided to do with it.
    regular_client.post('/admin/clients/add', data={})
    with app.app_context():
        rows = ActivityLog.query.filter_by(username='qa_regular').all()
        assert any(r.action.startswith('admin_add_client') for r in rows)
        assert all(r.role == 'regular_user' for r in rows)


def test_non_admin_cannot_view_or_clear_log(regular_client):
    resp = regular_client.get('/admin/log', follow_redirects=True)
    assert 'Нямате разрешение' in resp.get_data(as_text=True)
    resp = regular_client.post('/admin/log/clear', follow_redirects=True)
    assert 'Нямате разрешение' in resp.get_data(as_text=True)


def test_admin_clear_wipes_log_but_logs_itself(app, admin_client):
    admin_client.get('/admin/log')  # GET, not logged, just to have visited the page
    admin_client.post('/admin/users')  # noise, likely 404/405 but harmless
    with app.app_context():
        assert ActivityLog.query.count() > 0

    admin_client.post('/admin/log/clear')

    with app.app_context():
        rows = ActivityLog.query.all()
        # Only the clear action itself (and whatever ran after it) should remain
        assert any(r.action == 'admin_log_clear' for r in rows)
        assert not any('admin_users' in r.action for r in rows)


def test_export_returns_xlsx(admin_client):
    resp = admin_client.get('/admin/log/export')
    assert resp.status_code == 200
    assert resp.mimetype == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
