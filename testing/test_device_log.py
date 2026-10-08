"""
pytest for /admin/device-log ("Виж лог"): the recorded rows of a Shelly meter, a Modbus DTSU, a Solis inverter, the heat pump and a
machine (every meter linked to it) - admin only, limit whitelist, unknown device 404.

Run with:
    pytest testing/test_device_log.py -v
"""
import atexit
import json
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)


def _cleanup():
    try:
        os.remove(_db_path)
    except OSError:
        pass  # Windows keeps it locked while SQLAlchemy's pool is open


atexit.register(_cleanup)
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

import app as appmod
from app import (app as flask_app, db, User, ShellyDevice, ShellyReadingLog, ModbusDevice, SolisReadingLog, HeatPumpReading,
                 Machine, TemperatureSensor, TemperatureReading, limiter)


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        for name, role in (('dl_admin', 'admin'), ('dl_worker', 'worker')):
            if not User.query.filter_by(username=name).first():
                db.session.add(User(username=name, password=generate_password_hash('x123456'), role=role))
        db.session.commit()
        db.session.remove()
    return flask_app.test_client()


def _login(client, name):
    client.post('/login', data={'username': name, 'password': 'x123456'})


_ids = []


def _seed():
    if _ids:
        return _ids[0]
    with flask_app.app_context():
        m = Machine(name='Лазер')
        sh = ShellyDevice(name='Мерач 1', host='10.0.0.5', connection_type='ip', machines=[m])
        dt = ModbusDevice(name='Табло', host='10.0.0.9', port=26, unit_id=1, device_type='dtsu666')
        so = ModbusDevice(name='Инвертор', host='10.0.0.7', port=502, unit_id=1, device_type='solis_s6')
        db.session.add_all([m, sh, dt, so])
        db.session.commit()
        for i in range(60):
            db.session.add(ShellyReadingLog(host='10.0.0.5', ts=1_000_000 + 60 * i, total_power=100.0 + i, total_energy=5.0 + i / 100,
                                            channels_json=json.dumps([{'label': 'Фаза A', 'act_power': 50.0}])))
        db.session.add(ShellyReadingLog(host='10.0.0.9:26', ts=1_000_000, total_power=7000.0, total_energy=1.0))
        db.session.add(SolisReadingLog(device_id=so.id, ts=1_000_000, ac_power=500, pv_power=0, battery_soc=89, battery_power=-100,
                                       temperature=14.7, battery_fault_bits=0, snapshot_json='{}'))
        db.session.add(HeatPumpReading(ts=1_000_000, data_json=json.dumps({'Temp. Aussen': 14.3, 'Verdichter': True, 'Betriebsart': 1})))
        db.session.commit()
        _ids.append((m.id, sh.id, dt.id, so.id))
        return _ids[0]


def test_logs_per_device_kind(client):
    mid, sh, dt, so = _seed()
    _login(client, 'dl_admin')
    page = client.get(f'/admin/device-log?kind=shelly&id={sh}').get_data(as_text=True)
    assert 'показани 50 от 60' in page and '159 W' in page            # newest first, default limit 50
    assert 'показани 60 от 60' in client.get(f'/admin/device-log?kind=shelly&id={sh}&limit=200').get_data(as_text=True)
    assert 'показани 50 от 60' in client.get(f'/admin/device-log?kind=shelly&id={sh}&limit=7').get_data(as_text=True)   # not a whitelisted limit
    assert '7000 W' in client.get(f'/admin/device-log?kind=modbus&id={dt}').get_data(as_text=True)                      # DTSU: filed under host:port
    assert '89 %' in client.get(f'/admin/device-log?kind=modbus&id={so}').get_data(as_text=True)
    assert 'да' in client.get('/admin/device-log?kind=heatpump').get_data(as_text=True)
    assert 'Мерач 1' in client.get(f'/admin/device-log?kind=machine&id={mid}').get_data(as_text=True)


def test_temperature_sensor_log(client):
    from datetime import datetime
    with flask_app.app_context():
        s = TemperatureSensor(name='Склад', mqtt_topic='shellies/ht1', sensor_type='shelly_ht_gen1')
        db.session.add(s)
        db.session.commit()
        sid = s.id
        seen = datetime(2026, 10, 7, 12, 0, 0)
        appmod._mqtt_temp_state['shellies/ht1'] = {'online': True, 'temperature': 18.5, 'humidity': 55.0, 'battery': 90, 'last_seen': seen}
        appmod._temp_log_tick(); db.session.commit()
        appmod._temp_log_tick(); db.session.commit()                          # nothing new reported: no second row
        assert TemperatureReading.query.filter_by(sensor_id=sid).count() == 1
        appmod._mqtt_temp_state['shellies/ht1']['last_seen'] = datetime(2026, 10, 7, 12, 15, 0)
        appmod._temp_log_tick(); db.session.commit()
        assert TemperatureReading.query.filter_by(sensor_id=sid).count() == 2
    _login(client, 'dl_admin')
    page = client.get(f'/admin/device-log?kind=sensor&id={sid}').get_data(as_text=True)
    assert '18.5 °C' in page and 'показани 2 от 2' in page


def test_convector_log_only_on_change(client):
    from app import Convector, ConvectorLog
    states = iter([True, True, False, None, False])                          # None = offline: neither logged nor treated as a change
    orig = appmod._shelly_convector_status
    appmod._shelly_convector_status = lambda c: {'online': True, 'is_on': next(states), 'power_w': 1500.0, 'error': None}
    try:
        with flask_app.app_context():
            c = Convector(name='Офис', host='10.0.0.77')
            db.session.add(c)
            db.session.commit()
            cid = c.id
            for _ in range(5):
                appmod._convector_log_tick(); db.session.commit()
            assert [r.is_on for r in ConvectorLog.query.filter_by(convector_id=cid).order_by(ConvectorLog.id)] == [True, False]
    finally:
        appmod._shelly_convector_status = orig
    _login(client, 'dl_admin')
    page = client.get(f'/admin/device-log?kind=convector&id={cid}').get_data(as_text=True)
    assert 'Включен' in page and 'Изключен' in page and 'показани 2 от 2' in page


def test_energy_report(client):
    _, sh, dt, so = _seed()
    _login(client, 'dl_admin')
    rep = lambda key, extra='': client.get(f'/admin/energy-report?key={key}&period=custom&from=1970-01-05&to=2030-01-01{extra}')
    j = rep('10.0.0.5').get_json()
    assert len(j['series']['power']) > 0 and j['series']['power'][0][1] == 100 and 'solar' in j['cost']
    assert j['cost']['total']['kwh'] > 0
    assert rep('10.0.0.9:26').get_json()['series']['power'][0][1] == 7000                              # DTSU, keyed host:port
    assert rep('10.0.0.7:502').status_code == 404                                                      # an inverter is not a consumer
    assert client.get('/admin/energy-report?key=10.0.0.5&period=custom').get_json()['error']
    assert 'data-energy-panel' in client.get(f'/admin/device-log?kind=shelly&id={sh}').get_data(as_text=True)


def test_convector_power_and_battery_report(client):
    from app import Convector, ShellyReadingLog, SolisReadingLog
    _, _, _, so = _seed()
    orig = appmod._shelly_convector_status
    appmod._shelly_convector_status = lambda c: {'online': True, 'is_on': True, 'power_w': 1200.0, 'error': None}
    try:
        with flask_app.app_context():
            c = Convector(name='Склад', host='10.0.0.88')
            db.session.add(c)
            db.session.commit()
            cid = c.id
            appmod._convector_log_tick(); db.session.commit()
            assert ShellyReadingLog.query.filter_by(host=f'conv:{cid}').one().total_power == 1200.0
            db.session.add(SolisReadingLog(device_id=so, ts=1_000_100, battery_soc=50, battery_power=100, snapshot_json=json.dumps(
                {'battery': {'soc': 50, 'power': 100, 'voltage': 50, 'current': 2}, 'battery_groups': [{}, {'soc': 70, 'power': 300, 'voltage': 52, 'current': 6}]})))
            db.session.commit()
    finally:
        appmod._shelly_convector_status = orig
    _login(client, 'dl_admin')
    r = client.get(f'/admin/energy-report?key=conv:{cid}&period=today')
    assert r.status_code == 200 and 'cost' in r.get_json()
    j = client.get(f'/admin/energy-report?key=battery:{so}&period=custom&from=1970-01-05&to=2030-01-01').get_json()
    row = [p for p in j['series']['power'] if p[1] == 400]
    assert row and j['cost'] is None and [p[1] for p in j['series']['soc']].count(60.0) == 1


def test_log_access_and_missing(client):
    _, sh, _, _ = _seed()
    assert client.get(f'/admin/device-log?kind=shelly&id={sh}').status_code in (302, 401)       # anonymous -> login
    _login(client, 'dl_worker')
    assert client.get(f'/admin/device-log?kind=shelly&id={sh}', follow_redirects=False).status_code == 302
    client = flask_app.test_client()
    _login(client, 'dl_admin')
    assert client.get('/admin/device-log?kind=shelly&id=99999').status_code == 404
    assert client.get('/admin/device-log?kind=nonsense').status_code == 404
