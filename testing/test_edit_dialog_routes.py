"""
pytest: routes added/extended so an edit dialog (static/js/form_dialog.js)
can save a whole row in one POST - admin_update_user() (role + client
together, own role still locked) and admin_power_rename_device() taking the
machines checklist when the dialog sends with_machines=1.

Run with:
    pytest testing/test_edit_dialog_routes.py -v
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
        pass  # Windows keeps the file locked while SQLAlchemy's pooled connection is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Client, Machine, ShellyDevice, limiter


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def test_update_user_sets_role_and_client_together(admin_client):
    client = Client(name='QA Client')
    user = User(username='qa_worker', password='x', role='regular_user')
    db.session.add_all([client, user])
    db.session.commit()
    admin_client.post(f'/admin/users/{user.id}/update', data={'role': 'worker', 'client_id': str(client.id)})
    db.session.refresh(user)
    assert user.role == 'worker' and user.client_id == client.id

    admin_client.post(f'/admin/users/{user.id}/update', data={'role': 'worker', 'client_id': ''})
    db.session.refresh(user)
    assert user.client_id is None


def test_update_user_cannot_change_own_role(admin_client):
    me = User.query.filter_by(username='qa_admin').one()
    admin_client.post(f'/admin/users/{me.id}/update', data={'role': 'regular_user', 'client_id': ''})
    db.session.refresh(me)
    assert me.role == 'admin'


def test_rename_device_with_machines_checklist(admin_client):
    m1, m2 = Machine(name='QA M1'), Machine(name='QA M2')
    d = ShellyDevice(name='QA Meter', host='10.9.9.9')
    db.session.add_all([m1, m2, d])
    db.session.commit()
    admin_client.post(f'/admin/power/devices/{d.id}/rename', data={
        'name': 'QA Meter 2', 'connection_type': 'ip', 'host': '10.9.9.9',
        'with_machines': '1', 'machine_ids': [str(m1.id), str(m2.id)]})
    db.session.refresh(d)
    assert d.name == 'QA Meter 2' and {m.id for m in d.machines} == {m1.id, m2.id}

    # without with_machines the links are left alone
    admin_client.post(f'/admin/power/devices/{d.id}/rename', data={'name': 'QA Meter 3', 'connection_type': 'ip', 'host': '10.9.9.9'})
    db.session.refresh(d)
    assert d.name == 'QA Meter 3' and len(d.machines) == 2


def test_create_url_targets_admin_only(admin_client):
    # every entity's "+" target is a real, loadable admin page with its
    # "+ Нов ..." dialog requested; non-admins get no button at all
    from flask_login import login_user
    from app import CREATE_NEW_TARGETS, create_url
    for entity in CREATE_NEW_TARGETS:
        with flask_app.test_request_context():
            login_user(User.query.filter_by(username='qa_admin').one())
            url = create_url(entity)
        assert url, entity
        assert admin_client.get(url).status_code == 200, (entity, url)
    worker = User(username='qa_worker2', password='x', role='worker')
    db.session.add(worker)
    db.session.commit()
    with flask_app.test_request_context():
        login_user(worker)
        assert create_url('material') == ''
    with flask_app.test_request_context():
        assert create_url('material') == ''  # anonymous
