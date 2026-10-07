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

    m = HallMachine.query.filter_by(no=1).one()
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
    # an absurd position is clamped, not rejected; a machine may stand outside the first hall (a second hall)
    r = admin_client.post('/admin/hall/save', json=m.as_dict() | {'x': 999})
    assert r.get_json()['x'] == 80.0
    assert admin_client.post('/admin/hall/save', json=m.as_dict() | {'x': 60, 'z': 40}).get_json()['z'] == 40.0
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
    m = HallMachine.query.filter_by(no=4).one()          # B8 lathe: seeded with a 3.5 m bar feeder
    assert (m.acc_length, m.width) == (3.5, 2.4)
    r = admin_client.post('/admin/hall/save', json=m.as_dict() | {'acc_length': 4.2, 'acc_width': 0.6, 'acc_height': 0.9})
    d = r.get_json()
    assert r.status_code == 200 and (d['acc_length'], d['acc_width'], d['acc_height']) == (4.2, 0.6, 0.9) and d['width'] == 2.4
    assert admin_client.post('/admin/hall/save', json=m.as_dict() | {'acc_length': 0}).get_json()['acc_length'] == 0


def test_sequential_numbering_and_renumber(admin_client):
    admin_client.get('/admin/hall')
    nos = [m.no for m in HallMachine.query.order_by(HallMachine.no)]
    assert nos == list(range(1, len(nos) + 1))
    body = {'name': 'Нова', 'category': 'util', 'x': 1, 'z': 1, 'width': 1, 'depth': 1, 'height': 1}
    d = admin_client.post('/admin/hall/save', json=body).get_json()
    assert d['no'] == len(nos) + 1                                  # blank number -> next in sequence
    assert admin_client.post('/admin/hall/save', json=body | {'no': 50}).get_json()['no'] == 50   # but editable
    first = HallMachine.query.filter_by(no=1).one()
    admin_client.post(f'/admin/hall/{first.id}/delete')               # leaves a gap at 1 ...
    m = admin_client.post('/admin/hall/renumber').get_json()
    assert sorted(m.values()) == list(range(1, len(m) + 1))             # ... closed again, 1..N


def test_accessory_side_and_name(admin_client):
    admin_client.get('/admin/hall')
    m = HallMachine.query.filter_by(no=4).one()
    assert (m.acc_side, m.acc_name) == ('left', 'Прътоподавател')
    d = admin_client.post('/admin/hall/save', json=m.as_dict() | {'acc_side': 'back', 'acc_name': '  Стружкоотвод '}).get_json()
    assert (d['acc_side'], d['acc_name']) == ('back', 'Стружкоотвод')
    assert admin_client.post('/admin/hall/save', json=m.as_dict() | {'acc_side': 'up'}).status_code == 400


def test_laser_cabin_model_is_selectable(admin_client):
    admin_client.get('/admin/hall')
    m = HallMachine.query.first()
    assert admin_client.post('/admin/hall/save', json=m.as_dict() | {'model': 'laser_cabin'}).get_json()['model'] == 'laser_cabin'
    assert admin_client.post('/admin/hall/save', json=m.as_dict() | {'model': 'robot'}).get_json()['model'] == 'robot'
    assert admin_client.post('/admin/hall/save', json=m.as_dict() | {'model': 'sheet_lift'}).get_json()['model'] == 'sheet_lift'
    assert admin_client.post('/admin/hall/save', json=m.as_dict() | {'model': 'nope'}).status_code == 400


def test_apply_card_size(admin_client):
    admin_client.get('/admin/hall')
    card = ServiceMachineCard(title='Тест абкант', page='hall', specs_text='Усилие: 40 тона\nГабарити (Д x Ш x В): 2870 x 1625 x 2800 мм')
    db.session.add(card)
    db.session.commit()
    m = HallMachine.query.filter_by(no=1).one()
    m.card_id, m.x, m.z, m.width, m.depth, m.rotation = card.id, 10.0, 5.0, 1.0, 1.0, 0
    db.session.commit()
    d = admin_client.post(f'/admin/hall/{m.id}/apply-card-size').get_json()
    assert (d['width'], d['depth'], d['height']) == (2.87, 1.625, 2.8) and d['x'] == round(10.5 - 1.435, 3)   # centre kept
    m.rotation = 90
    db.session.commit()
    d = admin_client.post(f'/admin/hall/{m.id}/apply-card-size').get_json()
    assert (d['width'], d['depth']) == (1.625, 2.87)                                                       # turned: length along Z
    card.specs_text = 'Усилие: 40 тона'
    db.session.commit()
    assert admin_client.post(f'/admin/hall/{m.id}/apply-card-size').status_code == 400


def test_machine_dossier_fields_and_files(admin_client, tmp_path):
    import io
    from app import HallMachineFile, app as _app
    _app.config['MACHINE_FILES_FOLDER'] = str(tmp_path)
    admin_client.get('/admin/hall')
    m = HallMachine.query.filter_by(no=1).one()
    assert admin_client.get(f'/admin/hall/{m.id}/dossier').status_code == 200

    r = admin_client.post(f'/admin/hall/{m.id}/dossier/save', data={'manufacturer': 'DMG', 'serial_number': 'S1', 'year': '2015', 'notes': 'ok'})
    assert r.status_code == 302 and (m.manufacturer, m.serial_number, m.year, m.notes) == ('DMG', 'S1', 2015, 'ok')
    admin_client.post(f'/admin/hall/{m.id}/dossier/save', data={'year': '15'})            # invalid year is refused
    assert m.year == 2015

    # a 17 MB "backup" gets through the global 16 MB cap, a Cyrillic file name is kept for display only
    big = io.BytesIO(b'x' * (17 * 1024 * 1024))
    r = admin_client.post(f'/admin/hall/{m.id}/files', data={'files': [(big, 'бекъп.zip'), (io.BytesIO(b'hello'), 'ръководство.pdf')],
                                                             'category': 'backup', 'note': 'test'}, content_type='multipart/form-data')
    assert r.status_code == 302
    files = HallMachineFile.query.filter_by(machine_id=m.id).order_by(HallMachineFile.id).all()
    assert [f.original_name for f in files] == ['бекъп.zip', 'ръководство.pdf'] and files[0].size == 17 * 1024 * 1024
    assert all(f.stored_name.isascii() and f.category == 'backup' for f in files)
    d = admin_client.get(f'/admin/hall/files/{files[1].id}/download')
    assert d.status_code == 200 and d.data == b'hello'
    d.close()                                                   # Windows keeps the file locked while the response is open

    # a machine with files in its dossier can't be deleted from the editor
    assert admin_client.post(f'/admin/hall/{m.id}/delete').status_code == 400
    for f in files:
        assert admin_client.post(f'/admin/hall/files/{f.id}/delete').status_code == 302
    assert HallMachineFile.query.count() == 0 and not any(os.path.exists(os.path.join(tmp_path, str(m.id), f.stored_name)) for f in files)
    assert admin_client.post(f'/admin/hall/{m.id}/delete').status_code == 200


def test_unified_machines_page_and_system_sync(admin_client, tmp_path):
    import io
    from app import app as _app, Machine, Service, ElectricalPanel, Room, Building
    _app.config['MACHINE_FILES_FOLDER'] = str(tmp_path)
    admin_client.get('/admin/hall')
    hm = HallMachine.query.filter_by(no=1).one()
    hm.serial_number = 'SN-777'
    db.session.commit()
    admin_client.post(f'/admin/hall/{hm.id}/files', data={'files': (io.BytesIO(b'abc'), 'a.txt'), 'category': 'manual'}, content_type='multipart/form-data')
    old = Machine(name='Стара машина без досие')                       # a plain Machine (old /machines page) shows up in the same list
    db.session.add(old)
    db.session.commit()

    page = admin_client.get('/machines').get_data(as_text=True)
    assert f'/admin/hall/{hm.id}/dossier' in page and 'SN-777' in page and '1 файла' in page and 'Стара машина без досие' in page
    assert admin_client.get('/admin/machines-dossiers').status_code == 302          # the old overview URL now lands here

    # a Machine without a dossier gets one; the dossier is not drawn in 3D
    r = admin_client.post(f'/admin/machines/create-dossier/{old.id}')
    d = HallMachine.query.filter_by(machine_id=old.id).one()
    assert r.status_code == 302 and d.on_plan is False and d.name == old.name
    import json
    editor = admin_client.get('/admin/hall').get_data(as_text=True).split('const MACHINES = ')[1]
    assert d.id not in [m['id'] for m in json.JSONDecoder().raw_decode(editor)[0]]          # dossier-only machines are not in the 3D editor

    # a dossier without a Machine can create one; the name is one field on both sides
    admin_client.post(f'/admin/hall/{hm.id}/link-machine', data={'new': '1'})
    mc = db.session.get(HallMachine, hm.id).machine
    assert mc is not None and mc.name == hm.name
    hm.name = 'Преименувана от досието'
    db.session.commit()
    assert db.session.get(Machine, mc.id).name == 'Преименувана от досието'
    mc.name = 'Преименувана от машините'
    db.session.commit()
    assert db.session.get(HallMachine, hm.id).name == 'Преименувана от машините'

    # status, panel and services are set from the dossier and read from the Machine side
    b = Building(name='Б'); db.session.add(b); db.session.flush()
    room = Room(name='Р', building_id=b.id); db.session.add(room); db.session.flush()
    panel = ElectricalPanel(name='Табло', room_id=room.id); sv = Service(name='Фрезоване', price_per_hour_eur=50)
    db.session.add_all([panel, sv]); db.session.commit()
    admin_client.post(f'/admin/hall/{hm.id}/dossier/system', data={'status': 'maintenance', 'panel_id': panel.id, 'service_ids': [sv.id],
                                                                   'machine_type': 'mill', 'last_maintenance': '2026-03-01', 'on_plan': '1'})
    mc = db.session.get(Machine, mc.id)
    assert (mc.status, mc.panel_id, [s.id for s in mc.services], mc.machine_type) == ('maintenance', panel.id, [sv.id], 'mill')
    assert mc.last_maintenance.strftime('%Y-%m-%d') == '2026-03-01'
    assert 'maintenance' in admin_client.get('/factory3d').get_data(as_text=True)          # the 3D page shows the Machine's status
    db.session.expire_all()                                                                 # (the test shares one session; a real request starts clean)
    admin_client.post(f'/machines/{mc.id}/delete')                                          # deleting the Machine keeps the dossier
    db.session.expire_all()
    assert db.session.get(HallMachine, hm.id).machine_id is None


def test_machine_connections(admin_client):
    from app import MachineConnection, NetworkHost, NetworkDevice
    admin_client.get('/admin/hall')
    hm = HallMachine.query.filter_by(no=1).one()
    host = NetworkHost(mac_address='AA:BB:CC:DD:EE:01', hostname='dmu75', ip_address='192.168.18.50')
    sw = NetworkDevice(name='Суич хале', vendor='mikrotik')
    db.session.add_all([host, sw])
    db.session.commit()
    r = admin_client.post(f'/admin/hall/{hm.id}/connections', data={'kind': 'profinet', 'label': 'X130', 'address': '192.168.18.50', 'port': '102',
                                                                  'network_host_id': host.id, 'switch_device_id': sw.id, 'switch_port': 'ether7'})
    assert r.status_code == 302
    c = MachineConnection.query.one()
    assert (c.kind, c.network_host_id, c.switch_device_id, c.switch_port) == ('profinet', host.id, sw.id, 'ether7')
    page = admin_client.get(f'/admin/hall/{hm.id}/dossier').get_data(as_text=True)
    assert 'X130' in page and 'dmu75' in page and 'ether7' in page
    admin_client.post(f'/admin/hall/{hm.id}/connections', data={'kind': 'nonsense', 'network_host_id': '999'})      # unknown kind/FK are tolerated
    assert MachineConnection.query.count() == 2 and MachineConnection.query.filter_by(kind='other').one().network_host_id is None
    assert admin_client.post(f'/admin/hall/connections/{c.id}/delete').status_code == 302 and MachineConnection.query.count() == 1


def test_admin_sees_full_machine_info_client_does_not(admin_client):
    from app import MachineConnection
    admin_client.get('/admin/hall')
    hm = HallMachine.query.filter_by(no=1).one()
    db.session.add(MachineConnection(hall_machine_id=hm.id, kind='profinet', label='X130-SECRET'))
    db.session.commit()
    page = admin_client.get('/factory3d').get_data(as_text=True)
    assert 'X130-SECRET' in page and 'id="mode-plan"' in page and 'id="mode-roof"' in page
    assert admin_client.get('/admin/hall/live').status_code == 200
    assert admin_client.get('/admin/hall').get_data(as_text=True).count("'plan'") >= 1
    db.session.add(User(username='qa_client', password=generate_password_hash('irrelevant123'), role='regular_user'))
    db.session.commit()
    from flask import g
    g.pop('_login_user', None)
    c = admin_client.application.test_client()
    c.post('/login', data={'username': 'qa_client', 'password': 'irrelevant123'})
    g.pop('_login_user', None)            # the fixture's app context is shared by both clients, so flask-login's cached user must be dropped
    r = c.get('/factory3d')
    assert r.status_code == 200, (r.status_code, r.headers.get('Location'))
    page = r.get_data(as_text=True)
    assert 'X130' not in page and 'mode-plan' not in page and 'mode-roof' not in page and 'const IS_ADMIN = false' in page
    g.pop('_login_user', None)
    assert c.get('/admin/hall/live').status_code in (302, 403)


def test_links_iot_meters_and_new_equipment_kinds(admin_client):
    import json
    from app import (ElectricalPanel, Building, Room, Machine, ShellyDevice, Convector, TemperatureSensor, NetworkDevice,
                     NetworkLink, HallEquipment)
    admin_client.get('/admin/hall')
    b = Building(name='Хале')
    db.session.add(b)
    db.session.flush()
    room = Room(name='Хале 1', building_id=b.id)
    db.session.add(room)
    db.session.flush()
    main = ElectricalPanel(name='Главно', room_id=room.id)
    db.session.add(main)
    db.session.flush()
    sub = ElectricalPanel(name='Подтабло', room_id=room.id, parent_panel_id=main.id)
    db.session.add(sub)
    hm = HallMachine.query.filter_by(no=1).one()
    mc = Machine(name='M1', panel_id=sub.id)
    hm.machine = mc
    sw1, sw2 = NetworkDevice(name='Суич 1', vendor='cisco'), NetworkDevice(name='Суич 2', vendor='cisco')
    conv = Convector(name='Конв', device_type='shelly_gen1', host='10.0.0.9', room_id=room.id)
    db.session.add_all([sw1, sw2, conv, TemperatureSensor(name='Сензор', mqtt_topic='t/1'), ShellyDevice(name='Метър', host='10.0.0.5')])
    db.session.flush()
    db.session.add(NetworkLink(device_a_id=sw1.id, device_b_id=sw2.id, link_type='copper'))
    db.session.commit()

    # every new kind can be placed and linked to its record
    for kind, ref in (('convector', conv.id), ('network', sw1.id), ('panel', sub.id), ('panel', main.id)):
        r = admin_client.post('/admin/hall/equipment/save', json={'kind': kind, 'ref_id': ref, 'x': 5, 'z': 3, 'width': 0.5, 'depth': 0.3, 'height': 0.5})
        assert r.status_code == 200, r.get_json()
    assert admin_client.post('/admin/hall/equipment/save', json={'kind': 'sensor', 'ref_id': 999, 'x': 1, 'z': 1, 'width': 0.1, 'depth': 0.1, 'height': 0.1}).status_code == 400

    page = admin_client.get('/factory3d').get_data(as_text=True)
    line = next(l for l in page.splitlines() if l.startswith('const LINKS'))
    links = json.loads(line.split('=', 1)[1].split(';', 1)[0])
    pairs = {(l['kind'], tuple(l['a']), tuple(l['b'])) for l in links}
    assert ('power', ('panel', main.id), ('panel', sub.id)) in pairs and ('power', ('panel', sub.id), ('machine', hm.id)) in pairs
    assert ('data', ('network', sw1.id), ('network', sw2.id)) in pairs

    # meters are attached to the machine from the map and show up in its info
    dev = ShellyDevice.query.one()
    r = admin_client.post(f'/admin/hall/{hm.id}/meters', json={'kind': 'shelly', 'id': dev.id, 'attach': True})
    assert r.get_json() == [{'kind': 'shelly', 'id': dev.id, 'name': 'Метър'}] and db.session.get(Machine, mc.id).shelly_devices == [dev]
    assert admin_client.post(f'/admin/hall/{hm.id}/meters', json={'kind': 'shelly', 'id': dev.id, 'attach': False}).get_json() == []
    assert admin_client.post(f'/admin/hall/{hm.id}/meters', json={'kind': 'shelly', 'id': 999, 'attach': True}).status_code == 400
    assert admin_client.get('/admin/hall/live').get_json().keys() >= {'machines', 'panels', 'convectors', 'batteries'}


def test_auto_place_devices_by_room_position(admin_client):
    from app import (ElectricalPanel, Building, Room, TemperatureSensor, NetworkDevice, HallShape, HallEquipment)
    admin_client.get('/admin/hall')
    b = Building(name='Хале')
    db.session.add(b)
    db.session.flush()
    room = Room(name='Хале 1', building_id=b.id)
    db.session.add(room)
    db.session.flush()
    shape = HallShape.query.filter_by(kind='room').first()
    shape.room_id = room.id
    p = ElectricalPanel(name='Т1', room_id=room.id, pos_x=50, pos_y=50)
    db.session.add_all([p, TemperatureSensor(name='С1', mqtt_topic='t/9', room_id=room.id), NetworkDevice(name='Суич', vendor='cisco')])
    db.session.commit()
    before = HallEquipment.query.count()
    assert admin_client.post('/admin/hall/auto-place').get_json()['created'] == 3
    panel = HallEquipment.query.filter_by(kind='panel', ref_id=p.id).one()
    assert abs(panel.x + panel.width / 2 - (shape.x + shape.width / 2)) < 0.01        # 50 % of the room canvas = middle of the room in metres
    assert HallEquipment.query.filter_by(kind='sensor').one().x >= shape.x
    assert HallEquipment.query.filter_by(kind='network').one().z < 0                   # no room -> service strip behind the back wall
    assert admin_client.post('/admin/hall/auto-place').get_json()['created'] == 0 and HallEquipment.query.count() == before + 3   # idempotent
    assert 'sensors' in admin_client.get('/admin/hall/live').get_json()
    import app as appmod                                                                  # a room's air temperature = its sensors' mean; a room without a sensor is left out
    appmod._mqtt_temp_state['t/9'] = {'online': True, 'temperature': 23.5, 'humidity': 50.0, 'battery': 90, 'last_seen': None}
    rooms = admin_client.get('/admin/hall/live').get_json()['rooms']
    assert rooms and set(rooms.values()) == {23.5}
    appmod._mqtt_temp_state.pop('t/9')
    assert admin_client.get('/admin/hall/live').get_json()['rooms'] == {}


def test_sun_endpoint_and_solar_strings_admin_only(admin_client):
    import json
    from app import ModbusDevice, SolarPanel
    inv = ModbusDevice(name='Solis 1', host='10.0.0.1', device_type='solis_s6')
    db.session.add(inv)
    db.session.flush()
    db.session.add_all([SolarPanel(inverter_device_id=inv.id, row=1, col=c, string_number=3) for c in (1, 2)])
    db.session.commit()
    d = admin_client.get('/api/hall/sun').get_json()
    assert len(d['day']) == 145 and -90 <= d['now']['alt'] <= 90 and 0 <= d['now']['az'] <= 360
    assert max(x['alt'] for x in d['day']) > 0 > min(x['alt'] for x in d['day'])          # the sun rises and sets over the day

    def solar_of(client):
        page = client.get('/factory3d').get_data(as_text=True)
        line = next(l for l in page.splitlines() if l.startswith('const SOLAR'))
        return json.loads(line.split('=', 1)[1].split(';', 1)[0])

    assert solar_of(admin_client)['strings'] == [[3, 3]] and solar_of(admin_client)['inv'][0]['name'] == 'Solis 1'
    from flask import g
    db.session.add(User(username='qa_c2', password=generate_password_hash('irrelevant123'), role='regular_user'))
    db.session.commit()
    g.pop('_login_user', None)
    c = admin_client.application.test_client()
    c.post('/login', data={'username': 'qa_c2', 'password': 'irrelevant123'})
    g.pop('_login_user', None)
    assert solar_of(c)['strings'] == [[None, None]]                                     # clients see the layout, not the string wiring


def test_positions_stay_in_sync_between_hall_room_map_and_scheme(admin_client):
    from app import ElectricalPanel, Building, Room, Machine, Convector, HallShape, HallEquipment
    admin_client.get('/admin/hall')
    b = Building(name='Хале')
    db.session.add(b)
    db.session.flush()
    r1, r2 = Room(name='Стая 1', building_id=b.id), Room(name='Стая 2', building_id=b.id)
    db.session.add_all([r1, r2])
    db.session.flush()
    shapes = HallShape.query.filter_by(kind='room').all()
    s1, s2 = shapes[0], shapes[1]
    s1.room_id, s2.room_id = r1.id, r2.id
    s1.x, s1.z, s1.width, s1.depth = 0, 0, 10, 10                       # two known, non-overlapping rooms
    s2.x, s2.z, s2.width, s2.depth = 20, 0, 10, 10
    panel = ElectricalPanel(name='Т1', room_id=r1.id)
    conv = Convector(name='К1', device_type='shelly_gen1', host='10.0.0.9', room_id=r1.id)
    hm = HallMachine.query.filter_by(no=1).one()
    hm.machine = Machine(name='M1')
    db.session.add_all([panel, conv])
    db.session.commit()

    # hall -> room map + scheme: a panel dragged into room 2 changes its room and both maps
    r = admin_client.post('/admin/hall/equipment/save', json={'kind': 'panel', 'ref_id': panel.id, 'x': 24.6, 'z': 4.9, 'width': 0.8, 'depth': 0.2, 'height': 1.2})
    assert r.status_code == 200
    db.session.expire_all()
    p = db.session.get(ElectricalPanel, panel.id)
    assert p.room_id == r2.id and abs(p.pos_x - 50) < 0.5 and abs(p.pos_y - 50) < 0.5
    assert abs(p.overview_pos_x - 25 / 56 * 100) < 0.5 and abs(p.overview_pos_y - 5 / 12 * 100) < 0.5

    # room map -> hall: dragging the panel on its room map moves it on the plan (and the scheme follows)
    assert admin_client.post(f'/admin/factory-map/panel/{panel.id}/position', data={'pos_x': '10', 'pos_y': '20'}).status_code == 200
    db.session.expire_all()
    eq = HallEquipment.query.filter_by(kind='panel', ref_id=panel.id).one()
    assert abs(eq.x + eq.width / 2 - 21) < 0.01 and abs(eq.z + eq.depth / 2 - 2) < 0.01
    assert abs(db.session.get(ElectricalPanel, panel.id).overview_pos_x - 21 / 56 * 100) < 0.5

    # scheme -> hall -> room map
    assert admin_client.post(f'/admin/factory-map/panel/{panel.id}/overview-position', data={'pos_x': '15', 'pos_y': '50'}).status_code == 200
    db.session.expire_all()
    eq = HallEquipment.query.filter_by(kind='panel', ref_id=panel.id).one()
    assert abs(eq.x + eq.width / 2 - 8.4) < 0.01 and abs(eq.z + eq.depth / 2 - 6) < 0.01
    assert db.session.get(ElectricalPanel, panel.id).room_id == r1.id                    # (8.4, 6) lies in room 1 now

    # a machine: hall -> its Machine's room map, and back
    admin_client.post('/admin/hall/save', json=hm.as_dict() | {'x': 3.0, 'z': 4.0, 'width': 2.0, 'depth': 2.0})
    db.session.expire_all()
    mc = db.session.get(HallMachine, hm.id).machine
    assert mc.room_id == r1.id and abs(mc.pos_x - 40) < 0.5 and abs(mc.pos_y - 50) < 0.5
    assert admin_client.post(f'/admin/factory-map/machine/{mc.id}/position', data={'pos_x': '90', 'pos_y': '10'}).status_code == 200
    db.session.expire_all()
    moved = db.session.get(HallMachine, hm.id)
    assert abs(moved.x + moved.width / 2 - 9) < 0.01 and abs(moved.z + moved.depth / 2 - 1) < 0.01

    # a convector placed by "sync" lands on its room map too
    db.session.add(HallEquipment(kind='convector', ref_id=conv.id, x=24.5, z=1.9, width=1.0, depth=0.2, height=0.45))
    db.session.commit()
    assert admin_client.post('/admin/hall/sync-maps').get_json()['synced'] >= 2
    db.session.expire_all()
    assert db.session.get(Convector, conv.id).room_id == r2.id


def test_sync_creates_rooms_machines_and_panels_on_the_maps(admin_client):
    from app import ElectricalPanel, Room, Machine, HallShape, HallEquipment, HALL_ROOM_NAME
    admin_client.get('/admin/hall')
    assert Room.query.count() == 0 and Machine.query.count() == 0                       # fresh install: nothing exists on the old pages yet
    rooms_on_plan = HallShape.query.filter_by(kind='room').count()
    db.session.add(HallEquipment(kind='panel', ref_id=None, name='Ново табло', x=30, z=5, width=0.8, depth=0.25, height=1.2, elevation=1.4))
    db.session.commit()

    out = admin_client.post('/admin/hall/sync-maps').get_json()
    assert out['rooms'] == rooms_on_plan and out['machines'] > 0 and out['panels'] == 1

    # every marked room has a Room (so a room map), every dossier on the plan a Machine placed in a room at a position
    assert all(sh.room_id for sh in HallShape.query.filter_by(kind='room'))
    machines = Machine.query.all()
    assert len(machines) == out['machines'] and all(m.room_id and m.pos_x is not None for m in machines)
    # the panel drawn without a record got one, in the room under it (or the general hall room), with room-map and scheme positions
    panel = ElectricalPanel.query.filter_by(name='Ново табло').one()
    assert panel.room_id and panel.pos_x is not None and abs(panel.overview_pos_x - 30 / 56 * 100) < 1.5
    assert HallEquipment.query.filter_by(name='Ново табло').one().ref_id == panel.id
    # running it again adds nothing
    again = admin_client.post('/admin/hall/sync-maps').get_json()
    assert (again['rooms'], again['machines'], again['panels']) == (0, 0, 0) and Machine.query.count() == len(machines)


def test_room_map_is_drawn_in_metres(admin_client):
    import json
    from app import Building, Room, Machine, ElectricalPanel, HallShape, HallEquipment
    admin_client.get('/admin/hall')
    b = Building(name='Хале')
    db.session.add(b)
    db.session.flush()
    room, lone = Room(name='Стая', building_id=b.id), Room(name='Без място', building_id=b.id)
    db.session.add_all([room, lone])
    db.session.flush()
    sh = HallShape.query.filter_by(kind='room').first()
    sh.room_id, sh.x, sh.z, sh.width, sh.depth = room.id, 10, 2, 8, 6
    hm = HallMachine.query.filter_by(no=1).one()
    hm.x, hm.z, hm.width, hm.depth = 12, 3, 2, 1.5
    hm.machine = Machine(name='M1', room_id=room.id)
    panel = ElectricalPanel(name='Т1', room_id=room.id)
    db.session.add(panel)
    db.session.flush()
    db.session.add(HallEquipment(kind='panel', ref_id=panel.id, x=11, z=2.2, width=0.8, depth=0.25, height=1.2))
    db.session.commit()

    page = admin_client.get(f'/admin/factory-map/room/{room.id}').get_data(as_text=True)
    assert 'aspect-ratio: 8.0 / 6.0' in page or 'aspect-ratio: 8 / 6' in page             # the canvas has the room's proportions
    plan = json.JSONDecoder().raw_decode(page.split('var ROOM_PLAN = ')[1])[0]
    assert plan['rect'] == {'x': 10, 'z': 2, 'w': 8, 'd': 6}
    assert [m['no'] for m in plan['machines']] == [1] and (plan['machines'][0]['w'], plan['machines'][0]['d']) == (2, 1.5)    # true footprint in metres
    assert [e['kind'] for e in plan['equipment']] == ['panel'] and all(0 <= s['x'] + s['w'] and s['x'] < 18 for s in plan['shapes'])
    # a room that has no place on the hall plan keeps the old percent map (no plan, page still renders)
    page = admin_client.get(f'/admin/factory-map/room/{lone.id}').get_data(as_text=True)
    assert 'var ROOM_PLAN = null' in page and 'aspect-ratio: 8' not in page


def test_sync_keep_mode_only_fills_in_what_is_missing(admin_client):
    from app import ElectricalPanel, Building, Room, HallShape, HallEquipment, _hall_sync_maps, _hall_auto_place
    admin_client.get('/admin/hall')
    b = Building(name='Хале')
    db.session.add(b)
    db.session.flush()
    room = Room(name='Стая', building_id=b.id)
    db.session.add(room)
    db.session.flush()
    sh = HallShape.query.filter_by(kind='room').first()
    sh.room_id, sh.x, sh.z, sh.width, sh.depth = room.id, 10, 2, 8, 6
    placed = ElectricalPanel(name='Има позиция', room_id=room.id, pos_x=11.0, pos_y=22.0, overview_pos_x=33.0, overview_pos_y=44.0)
    empty = ElectricalPanel(name='Без позиция', room_id=room.id)
    db.session.add_all([placed, empty])
    db.session.flush()
    db.session.add_all([HallEquipment(kind='panel', ref_id=placed.id, x=15, z=4, width=0.8, depth=0.25, height=1.2),
                        HallEquipment(kind='panel', ref_id=empty.id, x=12, z=3, width=0.8, depth=0.25, height=1.2)])
    db.session.commit()
    _hall_sync_maps(keep=True)
    _hall_auto_place(keep=True)
    db.session.commit()
    db.session.expire_all()
    p, e = db.session.get(ElectricalPanel, placed.id), db.session.get(ElectricalPanel, empty.id)
    assert (p.pos_x, p.pos_y, p.overview_pos_x, p.overview_pos_y) == (11.0, 22.0, 33.0, 44.0)       # existing positions are left alone
    assert e.pos_x is not None and abs(e.pos_x - (12.4 - 10) / 8 * 100) < 0.5 and e.overview_pos_x is not None      # only the empty one is filled in
    _hall_sync_maps()                                                                                 # the UI button overwrites
    db.session.commit()
    db.session.expire_all()
    assert abs(db.session.get(ElectricalPanel, placed.id).pos_x - (15.4 - 10) / 8 * 100) < 0.5


def test_building_shape_can_be_created_beyond_the_hall(admin_client):
    admin_client.get('/admin/hall')
    body = {'kind': 'building', 'name': 'Склад', 'x': 20, 'z': 40, 'width': 20, 'depth': 10, 'height': 5}
    r = admin_client.post('/admin/hall/shape/save', json=body)
    assert r.status_code == 200 and r.get_json()['kind'] == 'building' and r.get_json()['z'] == 40
    assert 'Склад' in admin_client.get('/factory3d').get_data(as_text=True)


def test_explicit_hierarchy_parent_and_parcel(admin_client):
    from app import HallShape, HallParcel
    admin_client.get('/factory3d')
    parcel = HallParcel.query.first()
    b = admin_client.post('/admin/hall/shape/save', json={'kind': 'building', 'name': 'Склад', 'x': 20, 'z': 40, 'width': 10, 'depth': 10,
                                                          'height': 5, 'parcel_id': parcel.id}).get_json()
    assert b['parcel_id'] == parcel.id
    room = HallShape.query.filter_by(kind='room').first()
    r = admin_client.post('/admin/hall/shape/save', json=room.as_dict() | {'parent_id': b['id']})
    assert r.get_json()['parent_id'] == b['id']
    assert admin_client.post('/admin/hall/shape/save', json=room.as_dict() | {'parent_id': 99999}).status_code == 400   # no such parent
    assert admin_client.post('/admin/hall/shape/save', json=room.as_dict() | {'parcel_id': 99999, 'kind': 'building'}).status_code == 400
    m = HallMachine.query.first()
    ok = admin_client.post('/admin/hall/save', json=m.as_dict() | {'parent_id': room.id}).get_json()
    assert ok['parent_id'] == room.id
    assert admin_client.post('/admin/hall/save', json=m.as_dict() | {'parent_id': ''}).get_json()['parent_id'] is None


def test_building_outline_fits_its_rooms(admin_client):
    from app import HallShape
    admin_client.get('/factory3d')
    b = admin_client.post('/admin/hall/shape/save', json={'kind': 'building', 'name': 'Склад', 'x': 0, 'z': 30, 'width': 5, 'depth': 5, 'height': 5}).get_json()
    assert (b['width'], b['depth']) == (5, 5)                      # no rooms yet: keeps its drawn size
    room = {'kind': 'room', 'name': 'Цех', 'x': 10, 'z': 32, 'width': 8, 'depth': 6, 'height': 3, 'parent_id': b['id']}
    r = admin_client.post('/admin/hall/shape/save', json=room).get_json()
    fit = r['buildings'][0]
    assert (fit['x'], fit['z'], fit['width'], fit['depth']) == (10, 32, 8, 6)
    r2 = admin_client.post('/admin/hall/shape/save', json=room | {'name': 'Цех 2', 'x': 20, 'z': 40, 'id': None}).get_json()
    fit = r2['buildings'][0]
    assert (fit['x'], fit['z'], fit['width'], fit['depth']) == (10, 32, 18, 14)   # spans both rooms
    d = admin_client.post(f"/admin/hall/shape/{r2['id']}/delete").get_json()
    assert d['buildings'][0]['width'] == 8                          # shrinks back after a room is deleted


def test_editor_and_factory_map_stay_in_sync(admin_client):
    from app import Building, Room, HallShape
    admin_client.get('/factory3d')
    b = admin_client.post('/admin/hall/shape/save', json={'kind': 'building', 'name': 'Склад', 'x': 0, 'z': 40, 'width': 9, 'depth': 9, 'height': 5}).get_json()
    assert Building.query.get(b['building_id']).name == 'Склад'
    r = admin_client.post('/admin/hall/shape/save', json={'kind': 'room', 'name': 'Цех А', 'x': 1, 'z': 41, 'width': 4, 'depth': 4, 'height': 3, 'parent_id': b['id']}).get_json()
    room = Room.query.get(r['room_id'])
    assert room.name == 'Цех А' and room.building_id == b['building_id']          # editor -> factory map
    admin_client.post(f"/admin/buildings/{b['building_id']}/rename", data={'name': 'Склад 2'})
    admin_client.post(f"/admin/rooms/{room.id}/rename", data={'name': 'Цех Б'})
    assert HallShape.query.get(b['id']).name == 'Склад 2' and HallShape.query.get(r['id']).name == 'Цех Б'   # factory map -> editor
    admin_client.post(f"/admin/rooms/{room.id}/delete")
    assert HallShape.query.get(r['id']).room_id is None                          # no dangling link
    page = admin_client.get('/factory3d').get_data(as_text=True)
    assert 'DB_BUILDINGS' in page


def test_deleting_a_room_with_a_wired_panel_does_not_crash(admin_client):
    from app import Building, Room, ElectricalPanel, PanelComponent, PanelWire
    b = Building(name='Хале'); db.session.add(b); db.session.flush()
    room = Room(name='Табла', building_id=b.id); db.session.add(room); db.session.flush()
    panel = ElectricalPanel(name='Т1', room_id=room.id); db.session.add(panel); db.session.flush()
    c1, c2 = (PanelComponent(panel_id=panel.id, name=n) for n in ('A', 'B'))
    db.session.add_all([c1, c2]); db.session.flush()
    db.session.add(PanelWire(panel_id=panel.id, from_component_id=c1.id, to_component_id=c2.id)); db.session.commit()
    r = admin_client.post(f'/admin/rooms/{room.id}/delete')
    assert r.status_code == 302 and ElectricalPanel.query.count() == 0 and PanelWire.query.count() == 0


def test_rooms_cannot_overlap_unless_at_different_heights(admin_client):
    admin_client.get('/factory3d')
    room = {'kind': 'room', 'name': 'А', 'x': 100 - 90, 'z': 40, 'width': 10, 'depth': 10, 'height': 3, 'floors': 1}
    a = admin_client.post('/admin/hall/shape/save', json=room)
    assert a.status_code == 200
    # same floor area, same height -> refused
    assert admin_client.post('/admin/hall/shape/save', json=room | {'name': 'Б', 'x': 15}).status_code == 400
    # only touching edges is fine
    assert admin_client.post('/admin/hall/shape/save', json=room | {'name': 'В', 'x': 20}).status_code == 200
    # overlapping in plan but standing on top of the first (above 3 m) -> allowed
    up = admin_client.post('/admin/hall/shape/save', json=room | {'name': 'Г', 'elevation': 3})
    assert up.status_code == 200
    # a 2-storey room from the floor covers that level -> refused again
    assert admin_client.post('/admin/hall/shape/save', json=room | {'name': 'Д', 'elevation': 0, 'floors': 2, 'x': 12}).status_code == 400
    # editing a room never clashes with itself
    assert admin_client.post('/admin/hall/shape/save', json=a.get_json() | {'name': 'А2'}).status_code == 200


def test_room_made_in_buildings_and_rooms_lands_on_the_plan(admin_client):
    from app import Building, Room, HallShape
    admin_client.get('/factory3d')
    b = Building(name='Ново хале'); db.session.add(b); db.session.commit()
    admin_client.post(f'/admin/buildings/{b.id}/rooms/create', data={'name': 'Цех 1'})      # created there -> placed on the plan at once
    room = Room.query.filter_by(name='Цех 1').one()
    shape = HallShape.query.filter_by(kind='room', room_id=room.id).one()
    bshape = HallShape.query.filter_by(kind='building', building_id=b.id).one()
    assert shape.parent_id == bshape.id and shape.width == 8 and not (0 <= shape.x < 56 and 0 <= shape.z < 12)   # beside the hall, not on it
    # a room that was made before: placed on request, and only once
    old = Room(name='Стар', building_id=b.id); db.session.add(old); db.session.commit()
    d = admin_client.post(f'/admin/hall/room/{old.id}/place').get_json()
    assert [x['name'] for x in d['shapes'] if x['kind'] == 'room'] == ['Стар']
    assert admin_client.post(f'/admin/hall/room/{old.id}/place').get_json()['shapes'] == []
    assert HallShape.query.filter_by(kind='room', name='Стар').count() == 1
    # the new room does not sit on top of the first one
    new = HallShape.query.filter_by(room_id=old.id).one()
    assert (new.x, new.z) != (shape.x, shape.z)


def test_room_walls_snap_together_within_half_a_metre(admin_client):
    admin_client.get('/factory3d')
    base = {'kind': 'room', 'name': 'А', 'x': 10, 'z': 40, 'width': 10, 'depth': 10, 'height': 3, 'floors': 1}
    assert admin_client.post('/admin/hall/shape/save', json=base).status_code == 200            # x 10..20, z 40..50
    b = admin_client.post('/admin/hall/shape/save', json=base | {'name': 'Б', 'x': 40}).get_json()    # far away; moved around below
    save = lambda **kw: admin_client.post('/admin/hall/shape/save', json=base | {'name': 'Б', 'id': b['id']} | kw).get_json()
    assert save(x=20.3)['x'] == 20                    # gap 0.3 -> touches
    assert save(x=19.7)['x'] == 20                    # overlap 0.3 -> taken back
    assert save(x=21)['x'] == 21                      # gap 1 m stays
    assert save(x=10, z=50.4)['z'] == 50              # also along Z
    assert save(x=20.3, z=50.3)['x'] == 20.3          # diagonal neighbour: no facing walls, nothing to snap
    assert save(x=20.3, z=40, elevation=3)['x'] == 20.3   # a room on another level is not a neighbour


def test_new_room_gets_walls_and_floor_that_follow_it(admin_client):
    from app import HallShape
    admin_client.get('/factory3d')
    room = {'kind': 'room', 'name': 'Офис', 'x': 10, 'z': 40, 'width': 8, 'depth': 6, 'height': 3, 'floors': 1}
    r = admin_client.post('/admin/hall/shape/save', json=room).get_json()
    env = r['envelope']
    assert sorted(e['kind'] for e in env) == ['floor', 'wall', 'wall', 'wall', 'wall']
    floor = next(e for e in env if e['kind'] == 'floor')
    assert (floor['x'], floor['z'], floor['width'], floor['depth']) == (10, 40, 8, 6)
    top = next(e for e in env if e['kind'] == 'wall' and e['floors'] == 1)
    assert (top['width'], top['height'], top['depth']) == (8, 3, 0.125)      # standard 12.5 cm wall, as tall as the room
    # moving / resizing the room moves the same shapes (no duplicates)
    moved = admin_client.post('/admin/hall/shape/save', json=r | {'x': 30, 'width': 10, 'floors': 2}).get_json()
    assert len(moved['envelope']) == 5 and {e['id'] for e in moved['envelope']} == {e['id'] for e in env}
    assert next(e for e in moved['envelope'] if e['kind'] == 'floor')['x'] == 30
    assert next(e for e in moved['envelope'] if e['kind'] == 'wall' and e['floors'] == 1)['height'] == 6   # two storeys
    # a room that already existed (drawn by hand) is not given walls on edit
    old = HallShape.query.filter_by(kind='room').filter(HallShape.id != r['id']).first()
    assert admin_client.post('/admin/hall/shape/save', json=old.as_dict() | {'name': 'Преименувано'}).get_json()['envelope'] == []
    # deleting the room takes its generated shapes with it
    d = admin_client.post(f"/admin/hall/shape/{r['id']}/delete").get_json()
    assert sorted(d['removed']) == sorted(e['id'] for e in env)
    assert HallShape.query.filter_by(model='auto').count() == 0


def test_low_room_gets_only_a_floor(admin_client):
    admin_client.get('/factory3d')
    room = {'kind': 'room', 'name': 'Плоча', 'x': 10, 'z': 40, 'width': 8, 'depth': 6, 'height': 0.1, 'floors': 1}
    r = admin_client.post('/admin/hall/shape/save', json=room).get_json()
    assert [e['kind'] for e in r['envelope']] == ['floor']                      # lower than 0.2 m: no walls
    up = admin_client.post('/admin/hall/shape/save', json=r | {'height': 3}).get_json()
    assert sorted(e['kind'] for e in up['envelope']) == ['floor', 'wall', 'wall', 'wall', 'wall']   # grows -> walls appear
    down = admin_client.post('/admin/hall/shape/save', json=up | {'height': 0.15}).get_json()
    assert [e['kind'] for e in down['envelope']] == ['floor'] and len(down['removed']) == 4     # shrinks -> walls go


def test_envelopes_for_rooms_without_walls(admin_client):
    from app import HallShape
    admin_client.get('/factory3d')
    seeded = HallShape.query.filter_by(kind='room').count()
    # a room drawn by hand with nothing around it, and one with a hand-drawn wall along its edge
    bare = HallShape(kind='room', name='Гол', x=10, z=40, width=8, depth=6, height=3)
    walled = HallShape(kind='room', name='Със стена', x=30, z=40, width=8, depth=6, height=3)
    wall = HallShape(kind='wall', name='', x=30, z=40, width=8, depth=0.125, height=3)
    inner = HallShape(kind='wall', name='', x=13, z=42, width=0.125, depth=2, height=3)       # a partition inside the bare room: not a wall of it
    db.session.add_all([bare, walled, wall, inner]); db.session.commit()
    d = admin_client.post('/admin/hall/rooms/envelopes').get_json()
    assert 'Гол' in d['rooms'] and 'Със стена' not in d['rooms']
    assert sorted(e['kind'] for e in d['envelope'] if e['parent_id'] == bare.id) == ['floor', 'wall', 'wall', 'wall', 'wall']
    assert admin_client.post('/admin/hall/rooms/envelopes').get_json()['rooms'] == []          # second run: nothing left to do
    assert HallShape.query.filter_by(kind='room').count() == seeded + 2


def test_machine_data_sync_carries_the_hierarchy(admin_client, tmp_path, monkeypatch):
    """export -> (ids shift, like on another database) -> import: parents, parcels and buildings are matched by key, not by id."""
    from app import HallShape, HallParcel, HallMachine, Building
    from migration import machine_data_sync as sync
    monkeypatch.setattr(sync, 'PATH', str(tmp_path / 'm.json'))
    admin_client.get('/factory3d')
    parcel = HallParcel.query.filter_by(cadnum='72343.500.3037').one()
    b = admin_client.post('/admin/hall/shape/save', json={'kind': 'building', 'name': 'Ново хале', 'x': 0, 'z': 30, 'width': 1, 'depth': 1,
                                                          'height': 5, 'parcel_id': parcel.id}).get_json()
    r = admin_client.post('/admin/hall/shape/save', json={'kind': 'room', 'name': 'Цех 7', 'x': 20, 'z': 30, 'width': 8, 'depth': 6, 'height': 3, 'parent_id': b['id']}).get_json()
    m = HallMachine.query.first()
    admin_client.post('/admin/hall/save', json=m.as_dict() | {'name': 'Машина 17', 'id': None, 'no': 17, 'parent_id': r['id']})
    sync.export()
    import json                                                    # another database: one more shape in front -> every id is shifted by one
    data = json.load(open(sync.PATH, encoding='utf-8'))
    data['shapes'].insert(0, dict(data['shapes'][0]))
    for row in data['shapes'] + data['equipment'] + data['hall']:
        if isinstance(row.get('_parent'), int):
            row['_parent'] += 1
    json.dump(data, open(sync.PATH, 'w', encoding='utf-8'), ensure_ascii=False)
    HallShape.query.delete()
    HallParcel.query.delete()
    db.session.commit()
    sync.import_()
    nb = HallShape.query.filter_by(name='Ново хале').one()
    nr = HallShape.query.filter_by(name='Цех 7').one()
    assert nr.parent_id == nb.id and nr.parent_id != b['id']
    assert db.session.get(HallParcel, nb.parcel_id).cadnum == '72343.500.3037'
    assert db.session.get(Building, nb.building_id).name == 'Ново хале'
    assert HallMachine.query.filter_by(name='Машина 17').one().parent_id == nr.id
