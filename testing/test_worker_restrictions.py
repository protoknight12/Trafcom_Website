"""
pytest: what the `worker` role may no longer do - record/edit/delete delivery notes (supplier and
client: they move stock and set material prices) and set/change a device's network address
(convector host/MQTT topic/connection, temperature-sensor MQTT topic, creating a device at all).
Workers keep viewing those pages, renaming/moving a device and switching a convector on/off.

Run with:
    pytest testing/test_worker_restrictions.py -v
"""
import json

import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, MaterialPrice, DeliveryNote, ClientDeliveryNote, Client,
                 Convector, TemperatureSensor, limiter, seed_material_types)
import app as app_module


class _Client:
    """Test client that forgets flask-login's per-context user cache before each request: the fixture keeps one
    app context pushed, which the requests would otherwise share (and so the first user logged in would be everyone)."""

    def __init__(self, client):
        self._c = client

    def __getattr__(self, name):
        f = getattr(self._c, name)
        if name not in ('get', 'post'):
            return f

        def call(*a, **kw):
            g.pop('_login_user', None)
            return f(*a, **kw)
        return call


@pytest.fixture
def clients():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.add(User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker'))
        db.session.add(MaterialPrice(key='steel', display_name='Стомана 2мм', cost_per_m2=0.0,
                                     cutting_speed_mm_per_min=2000, pierce_rate_per_min=30, type='sheets',
                                     thickness_mm=2.0, sheet_width_mm=1000, sheet_length_mm=2000))
        db.session.add(Client(name='Клиент ООД'))
        db.session.commit()
        out = {}
        for name in ('admin', 'worker'):
            c = _Client(flask_app.test_client())
            c.post('/login', data={'username': f'qa_{name}', 'password': 'irrelevant123'})
            out[name] = c
        yield out
        db.session.remove()
        db.drop_all()


def _supplier_line(qty=10, price=40):
    return {'type': 'material', 'name': 'Стомана 2мм', 'material_key': 'steel', 'qty': qty, 'unit_price': price,
            'width': 1000, 'height': 2000, 'thickness': 2.0, 'material_type': 'sheets', 'item_id': None}


def _post_lines(client, url, lines, **header):
    return client.post(url, data={'items_json': json.dumps(lines), **header})


def _steel_stock():
    db.session.expire_all()
    return MaterialPrice.query.filter_by(key='steel').one().stock_quantity or 0


def _client_line(qty):
    steel = MaterialPrice.query.filter_by(key='steel').one()
    return {'type': 'material', 'target_id': steel.id, 'qty': qty, 'unit_price': 1}


# ---------- delivery notes ----------

def test_worker_cannot_write_supplier_notes(clients):
    admin, worker = clients['admin'], clients['worker']
    _post_lines(admin, '/admin/delivery-notes/create', [_supplier_line()])
    note = DeliveryNote.query.one()
    assert _steel_stock() == 10

    _post_lines(worker, '/admin/delivery-notes/create', [_supplier_line(5, 99)])
    _post_lines(worker, f'/admin/delivery-notes/{note.id}/edit', [_supplier_line(1, 99)])
    worker.post(f'/admin/delivery-notes/{note.id}/delete')

    db.session.expire_all()
    assert DeliveryNote.query.count() == 1 and _steel_stock() == 10
    assert DeliveryNote.query.one().items[0].quantity == 10
    assert MaterialPrice.query.count() == 1  # no catalog row created from the note either


def test_admin_still_writes_supplier_notes(clients):
    admin = clients['admin']
    _post_lines(admin, '/admin/delivery-notes/create', [_supplier_line()])
    note = DeliveryNote.query.one()
    _post_lines(admin, f'/admin/delivery-notes/{note.id}/edit', [_supplier_line(4, 40)])
    assert _steel_stock() == 4
    admin.post(f'/admin/delivery-notes/{DeliveryNote.query.one().id}/delete')
    db.session.expire_all()
    assert DeliveryNote.query.count() == 0 and _steel_stock() == 0


def test_worker_cannot_write_client_notes(clients):
    admin, worker = clients['admin'], clients['worker']
    cl = Client.query.one()
    _post_lines(admin, '/admin/delivery-notes/create', [_supplier_line(10, 40)])
    _post_lines(admin, '/admin/client-delivery-notes/create', [_client_line(3)], client_id=str(cl.id))
    note = ClientDeliveryNote.query.one()
    assert _steel_stock() == 7

    _post_lines(worker, '/admin/client-delivery-notes/create', [_client_line(2)], client_id=str(cl.id))
    _post_lines(worker, f'/admin/client-delivery-notes/{note.id}/edit', [_client_line(1)], client_id=str(cl.id))
    worker.post(f'/admin/client-delivery-notes/{note.id}/delete')

    db.session.expire_all()
    assert ClientDeliveryNote.query.count() == 1 and _steel_stock() == 7

    # an admin still can
    admin.post(f'/admin/client-delivery-notes/{note.id}/delete')
    db.session.expire_all()
    assert ClientDeliveryNote.query.count() == 0 and _steel_stock() == 10


def test_delivery_note_pages_controls_by_role(clients):
    admin, worker = clients['admin'], clients['worker']
    cl = Client.query.one()
    _post_lines(admin, '/admin/delivery-notes/create', [_supplier_line()])
    _post_lines(admin, '/admin/client-delivery-notes/create', [_client_line(1)], client_id=str(cl.id))
    sn, cn = DeliveryNote.query.one().id, ClientDeliveryNote.query.one().id

    for url, marks in (('/admin/delivery-notes', (f'/admin/delivery-notes/{sn}/edit', f'/admin/delivery-notes/{sn}/delete',
                                                  '/admin/delivery-notes/create')),
                       ('/admin/client-delivery-notes', (f'/admin/client-delivery-notes/{cn}/edit',
                                                         f'/admin/client-delivery-notes/{cn}/delete',
                                                         '/admin/client-delivery-notes/create'))):
        w = worker.get(url)
        assert w.status_code == 200
        body = w.get_data(as_text=True)
        assert '/print' in body  # the note itself stays readable
        for m in marks:
            assert m not in body, (url, m)
        a = admin.get(url).get_data(as_text=True)
        for m in marks:
            assert m in a, (url, m)
    # the "quick request" shortcut on the stock pages starts a note: workers don't get it
    assert 'Бърза заявка' not in worker.get('/storage/materials').get_data(as_text=True)
    assert 'Бърза заявка' in admin.get('/storage/materials').get_data(as_text=True)


# ---------- device addresses ----------

def _convector():
    c = Convector(name='Офис', connection_type='ip', host='192.168.18.90', device_type='shelly_gen1', relay_channel=0)
    db.session.add(c)
    db.session.commit()
    return c.id


def test_worker_cannot_create_devices(clients):
    worker, admin = clients['worker'], clients['admin']
    worker.post('/admin/convectors/create', data={'name': 'Нов', 'connection_type': 'ip', 'host': '10.0.0.1'})
    worker.post('/admin/temperature-sensors/create', data={'name': 'Нов', 'mqtt_topic': 'shellies/x'})
    assert Convector.query.count() == 0 and TemperatureSensor.query.count() == 0
    admin.post('/admin/convectors/create', data={'name': 'Нов', 'connection_type': 'ip', 'host': '10.0.0.1'})
    admin.post('/admin/temperature-sensors/create', data={'name': 'Нов', 'mqtt_topic': 'shellies/x'})
    assert Convector.query.count() == 1 and TemperatureSensor.query.count() == 1


def test_worker_convector_update_ignores_address(clients):
    worker, admin = clients['worker'], clients['admin']
    cid = _convector()
    r = worker.post(f'/admin/convectors/{cid}/update', data={
        'name': 'Преименуван', 'connection_type': 'mqtt', 'host': '10.9.9.9', 'mqtt_topic': 'evil/topic',
        'device_type': 'shelly_gen2', 'relay_channel': '3'})
    assert r.status_code == 302
    db.session.expire_all()
    c = db.session.get(Convector, cid)
    assert c.name == 'Преименуван'  # the shared form still works for the name/room/position part
    assert (c.connection_type, c.host, c.mqtt_topic, c.relay_channel) == ('ip', '192.168.18.90', None, 0)
    assert c.device_type == 'shelly_gen1'

    admin.post(f'/admin/convectors/{cid}/update', data={
        'name': 'Преименуван', 'connection_type': 'ip', 'host': '10.9.9.9', 'device_type': 'shelly_gen1', 'relay_channel': '0'})
    db.session.expire_all()
    assert db.session.get(Convector, cid).host == '10.9.9.9'


def test_convector_edit_window_address_readonly_for_worker(clients):
    cid = _convector()
    w = clients['worker'].get(f'/admin/convectors/{cid}/edit').get_data(as_text=True)
    a = clients['admin'].get(f'/admin/convectors/{cid}/edit').get_data(as_text=True)
    assert 'readonly' in w and 'disabled' in w
    assert 'readonly' not in a and 'disabled' not in a


def test_worker_sensor_update_keeps_topic(clients):
    worker, admin = clients['worker'], clients['admin']
    s = TemperatureSensor(name='Зала', mqtt_topic='shellies/ht1', sensor_type='shelly_ht_gen1')
    db.session.add(s)
    db.session.commit()
    sid = s.id
    worker.post(f'/admin/temperature-sensors/{sid}/update', data={
        'name': 'Нова зала', 'mqtt_topic': 'shellies/evil', 'sensor_type': 'shelly_ht_gen1', 'location_label': 'вън'})
    db.session.expire_all()
    s = db.session.get(TemperatureSensor, sid)
    assert (s.name, s.mqtt_topic, s.location_label) == ('Нова зала', 'shellies/ht1', 'вън')
    admin.post(f'/admin/temperature-sensors/{sid}/update', data={
        'name': 'Нова зала', 'mqtt_topic': 'shellies/ht2', 'sensor_type': 'shelly_ht_gen1'})
    db.session.expire_all()
    assert db.session.get(TemperatureSensor, sid).mqtt_topic == 'shellies/ht2'


def test_worker_can_still_toggle_convector(clients, monkeypatch):
    cid = _convector()
    sent = []
    monkeypatch.setattr(app_module, '_shelly_convector_status', lambda conv: {'online': True, 'is_on': False})
    monkeypatch.setattr(app_module, '_shelly_convector_set', lambda conv, on: sent.append(on))
    r = clients['worker'].post(f'/admin/convectors/{cid}/toggle')
    assert r.status_code == 302 and sent == [True]
