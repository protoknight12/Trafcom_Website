"""
pytest regression test for the Panel Generator preset endpoints
(/api/generator-presets*, /admin/generator-presets*): per-user save/overwrite,
ownership on delete, and the admin-only "copy another user's preset into my
own account" action. Uses the Flask test client (same pattern as
test_quick_create_material.py) since this needs real request/session/login
behavior.

Run with:
    pytest testing/test_generator_presets.py -v
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

from app import app as flask_app, db, User, GeneratorPreset, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        userA = User(username='qa_userA', password=generate_password_hash('irrelevant123'), role='regular_user')
        userB = User(username='qa_userB', password=generate_password_hash('irrelevant123'), role='regular_user')
        db.session.add_all([admin, userA, userB])
        db.session.commit()

    # Deliberately NOT held open for the yield: Flask's `g` (where Flask-Login
    # caches the loaded user) lives on the app context, not the request
    # context. Tests here run two logged-in clients (userA_client, userB_client)
    # in the same test - if an app context stayed pushed across both clients'
    # requests, the second client's requests would see the first client's
    # cached `g._login_user` instead of its own. Each test-client request
    # pushes and pops its own app context when none is already active, which
    # is what keeps the two sessions from bleeding into each other.
    yield flask_app

    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


def _client(app, username):
    c = app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


@pytest.fixture
def admin_client(app):
    return _client(app, 'qa_admin')


@pytest.fixture
def userA_client(app):
    return _client(app, 'qa_userA')


@pytest.fixture
def userB_client(app):
    return _client(app, 'qa_userB')


def test_save_then_list_roundtrip(userA_client):
    res = userA_client.post('/api/generator-presets', data={'name': 'My Preset', 'settings_json': '{"width": "1500"}'})
    assert res.get_json()['status'] == 'success'
    listed = userA_client.get('/api/generator-presets').get_json()['presets']
    assert len(listed) == 1
    assert listed[0]['name'] == 'My Preset'
    assert listed[0]['settings']['width'] == '1500'


def test_saving_same_name_overwrites_not_duplicates(userA_client):
    userA_client.post('/api/generator-presets', data={'name': 'X', 'settings_json': '{"width": "1000"}'})
    userA_client.post('/api/generator-presets', data={'name': 'X', 'settings_json': '{"width": "2000"}'})
    listed = userA_client.get('/api/generator-presets').get_json()['presets']
    assert len(listed) == 1
    assert listed[0]['settings']['width'] == '2000'


def test_users_only_see_their_own_presets(userA_client, userB_client):
    userA_client.post('/api/generator-presets', data={'name': 'A-only', 'settings_json': '{}'})
    userB_client.post('/api/generator-presets', data={'name': 'B-only', 'settings_json': '{}'})
    a_names = {p['name'] for p in userA_client.get('/api/generator-presets').get_json()['presets']}
    b_names = {p['name'] for p in userB_client.get('/api/generator-presets').get_json()['presets']}
    assert a_names == {'A-only'}
    assert b_names == {'B-only'}


def test_delete_rejects_non_owner(app, userA_client, userB_client):
    userA_client.post('/api/generator-presets', data={'name': 'A-only', 'settings_json': '{}'})
    with flask_app.app_context():
        preset_id = GeneratorPreset.query.filter_by(name='A-only').first().id

    res = userB_client.post(f'/api/generator-presets/{preset_id}/delete')
    assert res.status_code == 403
    with flask_app.app_context():
        assert GeneratorPreset.query.get(preset_id) is not None

    res = userA_client.post(f'/api/generator-presets/{preset_id}/delete')
    assert res.get_json()['status'] == 'success'
    with flask_app.app_context():
        assert GeneratorPreset.query.get(preset_id) is None


def test_generator_presets_dashboard_requires_admin(userA_client, admin_client):
    # role_required() redirects non-allowed roles with a flash rather than aborting 403.
    res = userA_client.get('/admin/generator-presets')
    assert res.status_code == 302
    assert res.headers['Location'].endswith('/dashboard')
    assert admin_client.get('/admin/generator-presets').status_code == 200


def test_admin_can_copy_preset_into_own_account(app, userA_client, admin_client):
    userA_client.post('/api/generator-presets', data={'name': 'Shared Look', 'settings_json': '{"width": "1500"}'})
    with flask_app.app_context():
        preset_id = GeneratorPreset.query.filter_by(name='Shared Look').first().id

    res = admin_client.post(f'/admin/generator-presets/{preset_id}/copy', follow_redirects=True)
    assert res.status_code == 200

    admin_presets = admin_client.get('/api/generator-presets').get_json()['presets']
    assert any(p['name'] == 'Shared Look' and p['settings']['width'] == '1500' for p in admin_presets)

    with flask_app.app_context():
        # Original owner's row is untouched - this created a second, independent row.
        assert GeneratorPreset.query.filter_by(name='Shared Look').count() == 2


def test_admin_copy_avoids_name_clash_with_own_existing_preset(app, userA_client, admin_client):
    userA_client.post('/api/generator-presets', data={'name': 'Dup', 'settings_json': '{}'})
    admin_client.post('/api/generator-presets', data={'name': 'Dup', 'settings_json': '{}'})
    with flask_app.app_context():
        source_id = GeneratorPreset.query.filter_by(name='Dup', user_id=User.query.filter_by(username='qa_userA').first().id).first().id

    admin_client.post(f'/admin/generator-presets/{source_id}/copy')
    admin_names = [p['name'] for p in admin_client.get('/api/generator-presets').get_json()['presets']]
    assert 'Dup' in admin_names
    assert 'Dup (копие)' in admin_names
