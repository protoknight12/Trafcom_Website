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
import json
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

from app import app as flask_app, db, User, ActivityLog, MaterialPrice, Detail, describe_changes, limiter


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


def test_composite_action_stores_expandable_details(app, admin_client):
    """
    Multi-item actions (delivery notes, orders, quick-create-product BOMs,
    offers) pass a details= breakdown to log_action() so the admin log
    dashboard can show more than the one-line action summary when a row is
    expanded - see api_quick_create_product(). The one-line action must stay
    short (just a count); details must actually list the item.
    """
    with app.app_context():
        material = MaterialPrice(key='qa_detail_mat', display_name='QA Material', cost_per_m2=5.0, type='sheets', erp_number=999100)
        db.session.add(material)
        db.session.commit()
        detail = Detail(name='QA Detail', material_key='qa_detail_mat', width=100, height=100,
                         total_length=10, pierce_count=1, calculated_price=12.5, erp_number=999101)
        db.session.add(detail)
        db.session.commit()
        detail_id = detail.id

    resp = admin_client.post('/api/quick-create-product', data={
        'name': 'QA Product', 'description': '', 'markup_percent': '10',
        'components_json': json.dumps([{'detail_id': detail_id, 'quantity': 3}]),
    })
    assert resp.status_code == 200

    with app.app_context():
        entry = ActivityLog.query.filter(ActivityLog.action.like('%QA Product%')).first()
        assert entry is not None
        assert 'детайл(а) в BOM' in entry.action  # one-line summary stays a count
        assert entry.details is not None
        assert 'QA Detail' in entry.details and '3 бр.' in entry.details

    # And it actually renders on the log page, collapsed by default.
    page = admin_client.get('/admin/log').get_data(as_text=True)
    assert 'QA Detail' in page
    assert 'hidden' in page  # details block starts collapsed


def test_simple_update_leaves_details_empty(app, admin_client):
    """A plain field-diff update (action text already says everything) should
    not get a details= blob - nothing to expand for those rows."""
    with app.app_context():
        material = MaterialPrice(key='qa_mat2', display_name='QA Mat 2', cost_per_m2=5.0,
                                  cutting_speed_mm_per_min=100, pierce_rate_per_min=10, type='sheets', erp_number=999102)
        db.session.add(material)
        db.session.commit()

    resp = admin_client.post('/admin/update_material/qa_mat2', data={
        'cost_per_m2': '7.5', 'cutting_speed_mm_per_min': '100', 'pierce_time_sec': '6',
        'type': 'sheets', 'brand': '', 'code_number': '',
    })
    assert resp.status_code in (302, 200)

    with app.app_context():
        entry = ActivityLog.query.filter(ActivityLog.action.like('%QA Mat 2%')).first()
        assert entry is not None
        assert entry.details is None


def test_describe_changes_diffs_pending_attribute_history(app):
    """
    describe_changes() (used by every admin_update_* route to build a plain-
    language log_action() message) relies on SQLAlchemy's dirty-attribute
    history - this guards that it actually finds changed fields, ignores
    unchanged ones, and reports "no changes" when nothing was touched.
    Mirrors the real route shape: fetch a fresh (already-persisted) row in
    its own query, then mutate it, same as e.g. admin_update_material() -
    mutating the same in-memory object right after creating/committing it
    (never happening in a real route) wouldn't have loaded-and-tracked
    attribute history to diff against.
    """
    with app.app_context():
        db.session.add(MaterialPrice(key='qa_mat', display_name='Стомана', cost_per_m2=10.0, type='sheets', erp_number=999001))
        db.session.commit()

        m = MaterialPrice.query.filter_by(key='qa_mat').first()
        m.display_name = 'Алуминий'
        m.cost_per_m2 = 15.5
        # brand left untouched - must not show up in the diff
        text = describe_changes('материал "Стомана"', m, {'display_name': 'име', 'cost_per_m2': 'цена', 'brand': 'марка'})
        assert 'Стомана' in text and 'Алуминий' in text
        assert '10.0' in text and '15.5' in text
        assert 'марка' not in text
        db.session.commit()

        m2 = MaterialPrice.query.filter_by(key='qa_mat').first()
        m2.display_name = m2.display_name  # reassigning the same value is not a change
        assert describe_changes('материал X', m2, {'display_name': 'име'}) == 'материал X (без промени)'
