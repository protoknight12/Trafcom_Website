"""
pytest: the hall 3D plan - /admin/hall editor endpoints (seed, move, link to a ServiceMachineCard,
validation, delete) and the client page /factory3d exposing the linked card.

Run with:
    pytest testing/test_hall_3d.py -v
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

from app import app as flask_app, db, User, ServiceMachineCard, HallMachine, HALL_SEED, limiter


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


def test_seed_move_link_and_client_page(admin_client):
    assert admin_client.get('/admin/hall').status_code == 200
    assert HallMachine.query.count() == len(HALL_SEED)

    card = ServiceMachineCard(title='Eckert карта', specs_text='Мощност: 6 kW', page='services')
    db.session.add(card)
    db.session.commit()

    m = HallMachine.query.filter_by(no=3).one()
    body = m.as_dict() | {'x': 7.5, 'z': 2.0, 'card_id': card.id}
    r = admin_client.post('/admin/hall/save', json=body)
    assert r.status_code == 200 and r.get_json()['x'] == 7.5

    page = admin_client.get('/factory3d').get_data(as_text=True)
    assert '"title": "Eckert \\u043a' in page.replace('"title":"', '"title": "') and '6 kW' in page  # tojson escapes Cyrillic


def test_validation_and_delete(admin_client):
    admin_client.get('/admin/hall')
    m = HallMachine.query.first()
    bad = m.as_dict() | {'name': ''}
    assert admin_client.post('/admin/hall/save', json=bad).status_code == 400
    bad = m.as_dict() | {'category': 'nope'}
    assert admin_client.post('/admin/hall/save', json=bad).status_code == 400
    # out-of-hall position is clamped, not rejected
    r = admin_client.post('/admin/hall/save', json=m.as_dict() | {'x': 999})
    assert r.get_json()['x'] == 56.0
    assert admin_client.post(f'/admin/hall/{m.id}/delete').status_code == 200
    assert HallMachine.query.count() == len(HALL_SEED) - 1


def test_shapes_seed_room_link_and_validation(admin_client):
    from app import HallShape, HALL_SHAPES_SEED, Building, Room
    assert admin_client.get('/admin/hall').status_code == 200
    assert HallShape.query.count() == len(HALL_SHAPES_SEED)

    b = Building(name='Хале')
    db.session.add(b)
    db.session.flush()
    room = Room(name='Струговна', building_id=b.id)
    db.session.add(room)
    db.session.commit()

    s = HallShape.query.filter_by(kind='room').first()
    r = admin_client.post('/admin/hall/shape/save', json=s.as_dict() | {'room_id': room.id})
    assert r.status_code == 200 and r.get_json()['label'] == 'Хале · Струговна'
    # a wall never keeps a room link
    w = HallShape.query.filter_by(kind='wall').first()
    assert admin_client.post('/admin/hall/shape/save', json=w.as_dict() | {'room_id': room.id}).get_json()['room_id'] is None
    assert admin_client.post('/admin/hall/shape/save', json=s.as_dict() | {'kind': 'nope'}).status_code == 400
    assert admin_client.post(f'/admin/hall/shape/{w.id}/delete').status_code == 200
    assert HallShape.query.count() == len(HALL_SHAPES_SEED) - 1
    assert admin_client.get('/factory3d').status_code == 200


def test_factory3d_passes_solar_slopes_per_inverter(admin_client):
    from app import ModbusDevice, SolarPanel
    invs = [ModbusDevice(name=f'Solis {i}', host=f'10.0.0.{i}', device_type='solis_s6') for i in (1, 2)]
    db.session.add_all(invs)
    db.session.flush()
    for inv in invs:
        db.session.add_all([SolarPanel(inverter_device_id=inv.id, row=r, col=c) for r in (1, 2) for c in (1, 2, 3)])
    db.session.commit()
    page = admin_client.get('/factory3d').get_data(as_text=True)
    line = next(l for l in page.splitlines() if l.startswith('const SOLAR'))
    import json
    solar = json.loads(line.split('=', 1)[1].split(';', 1)[0])
    assert [len(s) for s in solar['slopes']] == [6, 6] and solar['l'] > solar['w']


def test_equipment_elevation_link_and_room_floors(admin_client):
    from app import HallEquipment, HallShape, ElectricalPanel, Building, Room
    admin_client.get('/admin/hall')
    seeded = HallEquipment.query.count()          # battery stacks seeded on first visit
    b = Building(name='Хале')
    db.session.add(b)
    db.session.flush()
    room = Room(name='Ел. помещение', building_id=b.id)
    db.session.add(room)
    db.session.flush()
    panel = ElectricalPanel(name='Главно табло', room_id=room.id)
    db.session.add(panel)
    db.session.commit()

    body = {'kind': 'panel', 'ref_id': panel.id, 'name': '', 'x': 3, 'z': 4, 'width': 0.8, 'depth': 0.25, 'height': 1.2, 'elevation': 1.4}
    r = admin_client.post('/admin/hall/equipment/save', json=body)
    d = r.get_json()
    assert r.status_code == 200 and d['elevation'] == 1.4 and d['label'] == 'Главно табло'
    assert admin_client.post('/admin/hall/equipment/save', json=body | {'kind': 'nope'}).status_code == 400
    assert admin_client.post('/admin/hall/equipment/save', json=body | {'ref_id': 9999}).status_code == 400
    assert admin_client.get('/factory3d').status_code == 200

    s = HallShape.query.filter_by(kind='room').first()
    r = admin_client.post('/admin/hall/shape/save', json=s.as_dict() | {'floors': 3, 'height': 3.2, 'elevation': 0.5})
    assert (r.get_json()['floors'], r.get_json()['height'], r.get_json()['elevation']) == (3, 3.2, 0.5)
    assert admin_client.post(f"/admin/hall/equipment/{d['id']}/delete").status_code == 200
    assert HallEquipment.query.count() == seeded


def test_machine_accessory_sized_separately(admin_client):
    admin_client.get('/admin/hall')
    m = HallMachine.query.filter_by(no=6).one()          # B8 lathe: seeded with a 3.5 m bar feeder
    assert (m.acc_length, m.width) == (3.5, 2.4)
    r = admin_client.post('/admin/hall/save', json=m.as_dict() | {'acc_length': 4.2, 'acc_width': 0.6, 'acc_height': 0.9})
    d = r.get_json()
    assert r.status_code == 200 and (d['acc_length'], d['acc_width'], d['acc_height']) == (4.2, 0.6, 0.9) and d['width'] == 2.4
    assert admin_client.post('/admin/hall/save', json=m.as_dict() | {'acc_length': 0}).get_json()['acc_length'] == 0
