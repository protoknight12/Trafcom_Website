"""
pytest regression test for the ShellyDevice CRUD routes backing the
"Управление на машините" panel on /admin/power: admin_power_add_device(),
admin_power_delete_device(), admin_power_set_device_machines(). Uses the Flask
test client (same pattern as test_quick_create_material.py) since this needs
real request/session/login/CSRF behavior that a plain assert script can't
drive - unlike testing/test_shelly_status.py, which covers this integration's
pure functions without any of that.

ShellyDevice<->Machine is many-to-many (shelly_device_machines table): one
meter can feed several machines at once (a shared feed/sub-panel) and one
machine can have several meters, so machine_ids is always a list in these
requests, via werkzeug's MultiDict (data={'machine_ids': [id1, id2]} posts it
as repeated form fields, matching request.form.getlist('machine_ids') on the
route side).

Also guards that delete_machine() removes a linked ShellyDevice's association
row instead of raising an FK error - the one behavior change this feature
made to an existing route.

Run with:
    pytest testing/test_shelly_device_routes.py -v
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

from app import app as flask_app, db, User, Machine, ShellyDevice, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        worker = User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker')
        db.session.add_all([admin, worker])
        laser = Machine(name='QA Laser', status='idle')
        mill = Machine(name='QA Mill', status='running')
        db.session.add_all([laser, mill])
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
def worker_client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_worker', 'password': 'irrelevant123'})
    return c


def _machine_id(name):
    return Machine.query.filter_by(name=name).first().id


def test_add_device(admin_client):
    res = admin_client.post('/admin/power/devices/add',
                             data={'name': 'QA Meter', 'host': '10.0.0.5'},
                             follow_redirects=True)
    assert res.status_code == 200
    with flask_app.app_context():
        d = ShellyDevice.query.filter_by(host='10.0.0.5').first()
        assert d is not None and d.name == 'QA Meter' and d.machines == []


def test_add_device_defaults_name_to_host(admin_client):
    admin_client.post('/admin/power/devices/add', data={'name': '', 'host': '10.0.0.6'})
    with flask_app.app_context():
        d = ShellyDevice.query.filter_by(host='10.0.0.6').first()
        assert d.name == '10.0.0.6'


def test_add_device_strips_pasted_url(admin_client):
    admin_client.post('/admin/power/devices/add', data={'name': 'QA', 'host': 'http://10.0.0.7/'})
    with flask_app.app_context():
        assert ShellyDevice.query.filter_by(host='10.0.0.7').first() is not None
        assert ShellyDevice.query.filter_by(host='http://10.0.0.7/').first() is None


def test_add_device_rejects_blank_host(admin_client):
    admin_client.post('/admin/power/devices/add', data={'name': 'QA', 'host': '  '})
    with flask_app.app_context():
        assert ShellyDevice.query.filter_by(name='QA').first() is None


def test_add_device_rejects_duplicate_host(admin_client):
    admin_client.post('/admin/power/devices/add', data={'name': 'First', 'host': '10.0.0.8'})
    admin_client.post('/admin/power/devices/add', data={'name': 'Second', 'host': '10.0.0.8'})
    with flask_app.app_context():
        matches = ShellyDevice.query.filter_by(host='10.0.0.8').all()
        assert len(matches) == 1
        assert matches[0].name == 'First'  # the duplicate attempt didn't overwrite it


def test_add_device_with_single_machine_link(admin_client):
    with flask_app.app_context():
        laser_id = _machine_id('QA Laser')
    admin_client.post('/admin/power/devices/add',
                       data={'name': 'Linked Meter', 'host': '10.0.0.9', 'machine_ids': [str(laser_id)]})
    with flask_app.app_context():
        d = ShellyDevice.query.filter_by(host='10.0.0.9').first()
        assert [m.name for m in d.machines] == ['QA Laser']


def test_add_device_with_multiple_machine_links(admin_client):
    """One meter feeding a shared sub-panel/feed - the core reason this is many-to-many."""
    with flask_app.app_context():
        laser_id, mill_id = _machine_id('QA Laser'), _machine_id('QA Mill')
    admin_client.post('/admin/power/devices/add', data={
        'name': 'Shared Feed Meter', 'host': '10.0.0.16',
        'machine_ids': [str(laser_id), str(mill_id)],
    })
    with flask_app.app_context():
        d = ShellyDevice.query.filter_by(host='10.0.0.16').first()
        assert {m.name for m in d.machines} == {'QA Laser', 'QA Mill'}


def test_add_device_rejects_nonexistent_machine(admin_client):
    admin_client.post('/admin/power/devices/add',
                       data={'name': 'QA', 'host': '10.0.0.10', 'machine_ids': ['999999']})
    with flask_app.app_context():
        assert ShellyDevice.query.filter_by(host='10.0.0.10').first() is None


def test_set_add_and_unset_machine_links(admin_client):
    with flask_app.app_context():
        laser_id, mill_id = _machine_id('QA Laser'), _machine_id('QA Mill')
        d = ShellyDevice(name='Relink Test', host='10.0.0.11')
        db.session.add(d)
        db.session.commit()
        device_id = d.id

    admin_client.post(f'/admin/power/devices/{device_id}/set-machines', data={'machine_ids': [str(laser_id)]})
    with flask_app.app_context():
        assert [m.name for m in ShellyDevice.query.get(device_id).machines] == ['QA Laser']

    # replacing the set, not appending to it - resubmitting with both should
    # result in exactly two, not three
    admin_client.post(f'/admin/power/devices/{device_id}/set-machines',
                       data={'machine_ids': [str(laser_id), str(mill_id)]})
    with flask_app.app_context():
        assert {m.name for m in ShellyDevice.query.get(device_id).machines} == {'QA Laser', 'QA Mill'}

    # two meters CAN link to the same machine - no uniqueness constraint
    with flask_app.app_context():
        d2 = ShellyDevice(name='Second Relink Test', host='10.0.0.12')
        d2.machines = [Machine.query.get(laser_id)]
        db.session.add(d2)
        db.session.commit()
        assert ShellyDevice.query.filter(ShellyDevice.machines.any(id=laser_id)).count() == 2

    # empty machine_ids means fully unlinked, not "leave unchanged"
    admin_client.post(f'/admin/power/devices/{device_id}/set-machines', data={})
    with flask_app.app_context():
        assert ShellyDevice.query.get(device_id).machines == []


def test_delete_device(admin_client):
    with flask_app.app_context():
        d = ShellyDevice(name='To Delete', host='10.0.0.13')
        db.session.add(d)
        db.session.commit()
        device_id = d.id

    admin_client.post(f'/admin/power/devices/{device_id}/delete')
    with flask_app.app_context():
        assert ShellyDevice.query.get(device_id) is None


def test_delete_machine_detaches_linked_device_instead_of_erroring(admin_client):
    """
    Guards the one change this feature made to an existing route: deleting a
    Machine linked to a ShellyDevice must remove that association row (same
    effect Order/DxfFile already got via their nullable FKs), not raise an
    FK error, and must leave any OTHER machine link on that device intact.
    """
    with flask_app.app_context():
        detachable = Machine(name='QA Detachable', status='idle')
        survivor_machine = Machine(name='QA Survivor', status='idle')
        db.session.add_all([detachable, survivor_machine])
        db.session.commit()
        d = ShellyDevice(name='Attached Meter', host='10.0.0.14')
        d.machines = [detachable, survivor_machine]
        db.session.add(d)
        db.session.commit()
        machine_id, device_id, survivor_id = detachable.id, d.id, survivor_machine.id

    res = admin_client.post(f'/machines/{machine_id}/delete', follow_redirects=True)
    assert res.status_code == 200
    with flask_app.app_context():
        assert Machine.query.get(machine_id) is None
        survivor = ShellyDevice.query.get(device_id)
        assert survivor is not None
        assert [m.id for m in survivor.machines] == [survivor_id]


def test_worker_forbidden(worker_client):
    res = worker_client.post('/admin/power/devices/add', data={'name': 'QA', 'host': '10.0.0.15'})
    assert res.status_code == 302  # role_required redirects rather than 403ing
    with flask_app.app_context():
        assert ShellyDevice.query.filter_by(host='10.0.0.15').first() is None
