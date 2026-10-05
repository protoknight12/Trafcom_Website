"""Device <-> hall plan two-way link: a device created in a request gets its plan object; deleting the device removes it; a plan rename renames the device."""
import os
import tempfile

_fd, _path = tempfile.mkstemp(suffix='.db')
os.close(_fd)
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_path}'

import pytest
from werkzeug.security import generate_password_hash

from app import app, db, User, Camera, TemperatureSensor, HallEquipment


@pytest.fixture
def client():
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        u = User.query.filter_by(username='adm').first()
        if u is None:
            u = User(username='adm', password=generate_password_hash('Passw0rd!x'), role='admin')
            db.session.add(u); db.session.commit()
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid); s['_fresh'] = True
    return c


def test_new_sensor_is_placed_and_deleted_with_it(client):
    r = client.post('/admin/temperature-sensors/create', data={'name': 'T1', 'mqtt_topic': 'shellies/t1'})
    assert r.status_code in (302, 200)
    with app.app_context():
        s = TemperatureSensor.query.filter_by(name='T1').one()
        assert HallEquipment.query.filter_by(kind='sensor', ref_id=s.id).count() == 1
        sid = s.id
    client.post(f'/admin/temperature-sensors/{sid}/delete')
    with app.app_context():
        assert HallEquipment.query.filter_by(kind='sensor', ref_id=sid).count() == 0


def test_camera_created_in_cameras_page_is_placed(client):
    client.post('/admin/cameras/create', data={'name': 'C1', 'conn_type': 'rtsp', 'host': '10.0.0.9'})
    with app.app_context():
        c = Camera.query.filter_by(name='C1').one()
        assert HallEquipment.query.filter_by(kind='camera', ref_id=c.id).count() == 1
