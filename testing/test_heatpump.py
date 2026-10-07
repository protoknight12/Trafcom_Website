"""
pytest for the Heliotherm heat pump module: admin-only routes, converter-host
validation, the history endpoint, and _heatpump_fast()'s "-50 = no sensor"
mapping. The poller itself talks to real hardware and is not exercised here.

Run with:
    pytest testing/test_heatpump.py -v
"""
import atexit
import json
import os
import tempfile
import time

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
from app import app as flask_app, db, User, HeatPumpReading, limiter


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        for name, role in (('hp_admin', 'admin'), ('hp_worker', 'worker')):
            if not User.query.filter_by(username=name).first():
                db.session.add(User(username=name, password=generate_password_hash('x123456'), role=role))
        db.session.commit()
        db.session.remove()
    return flask_app.test_client()


def _login(c, name):
    c.post('/login', data={'username': name, 'password': 'x123456'})


def test_admin_only(client):
    _login(client, 'hp_worker')
    for url in ('/admin/heatpump', '/admin/heatpump/data', '/admin/heatpump/history'):
        assert client.get(url).status_code == 302


def test_page_data_and_host_validation(client):
    _login(client, 'hp_admin')
    assert client.get('/admin/heatpump').status_code == 200
    assert 'online' in client.get('/admin/heatpump/data').get_json()

    client.post('/admin/heatpump/host', data={'host': 'not a host'})
    with flask_app.app_context():
        assert appmod.get_text(appmod.HEATPUMP_HOST_KEY, '') == ''
    client.post('/admin/heatpump/host', data={'host': '192.168.18.123:23'})
    with flask_app.app_context():
        assert appmod.get_text(appmod.HEATPUMP_HOST_KEY, '') == '192.168.18.123:23'


def test_history_series(client):
    _login(client, 'hp_admin')
    with flask_app.app_context():
        db.session.add(HeatPumpReading(ts=int(time.time()) - 60,
                                       data_json=json.dumps({'Temp. Aussen': 15.3, 'Temp. Vorlauf': 38.6})))
        db.session.commit()
    series = client.get('/admin/heatpump/history?hours=1').get_json()
    assert series['Temp. Aussen'][0][1] == 15.3
    assert series['Temp. Brauchwasser'][0][1] is None


def test_fast_query_maps_missing_sensor():
    class FakeHp:
        def fast_query(self):
            return {'Temp. Brauchwasser': -50.0, 'Temp. Aussen': 15.3, 'Verdichter': True}
    assert appmod._heatpump_fast(FakeHp()) == {'Temp. Brauchwasser': None, 'Temp. Aussen': 15.3, 'Verdichter': True}


def _post_set(c, name, value):
    return c.post('/admin/heatpump/set', data={'name': name, 'value': value})


def test_set_validation(client, monkeypatch):
    _login(client, 'hp_admin')
    monkeypatch.setitem(appmod._heatpump_live, 'online', True)
    assert _post_set(client, 'Verdichter', '1').status_code == 400         # not whitelisted
    assert _post_set(client, 'HKR Soll_Raum', 'abc').status_code == 400    # not a number
    assert _post_set(client, 'HKR Soll_Raum', '40').status_code == 400     # above pump limit (25)
    assert _post_set(client, 'WW Normaltemp.', '48.5').status_code == 400  # INT parameter
    monkeypatch.setitem(appmod._heatpump_live, 'online', False)
    assert _post_set(client, 'HKR Soll_Raum', '22').status_code == 503     # offline: nothing queued
    assert not appmod._heatpump_cmds


def test_set_requires_admin(client):
    _login(client, 'hp_worker')
    assert _post_set(client, 'HKR Soll_Raum', '22').status_code == 302


def test_run_cmds_applies_confirms_and_drops_stale(monkeypatch):
    import threading
    class FakeHp:
        def __init__(self): self.sent = []
        def set_param(self, n, v): self.sent.append((n, v))
        def get_param(self, n): return self.sent[-1][1]
    def cmd(value, **kw):
        return dict({'name': 'HKR Soll_Raum', 'value': value, 'cancelled': False, 'done': threading.Event(),
                     'result': None, 'deadline': time.monotonic() + 30}, **kw)
    ok, cancelled, expired = cmd(22.0), cmd(21.0, cancelled=True), cmd(20.0, deadline=time.monotonic() - 1)
    appmod._heatpump_cmds.extend([cancelled, expired, ok])
    hp = FakeHp()
    appmod._heatpump_run_cmds(hp)
    assert hp.sent == [('HKR Soll_Raum', 22.0)]
    assert ok['done'].is_set() and ok['result'] == {'ok': True, 'value': 22.0}
    assert not cancelled['done'].is_set() and not expired['done'].is_set()


def _cfg(**kw):
    return dict({'meter': 'm', 'price_day': 0.20, 'price_night': 0.10, 'night_from': 22, 'night_to': 6}, **kw)


def test_tariff_day_night_and_wrap():
    from datetime import datetime
    ts = lambda h: int(datetime(2026, 1, 15, h, 30).timestamp())
    assert appmod._heatpump_tariff(ts(12), _cfg()) == 0.20
    assert appmod._heatpump_tariff(ts(23), _cfg()) == 0.10   # night window wraps midnight
    assert appmod._heatpump_tariff(ts(3), _cfg()) == 0.10
    assert appmod._heatpump_tariff(ts(6), _cfg()) == 0.20    # end hour is day again
    assert appmod._heatpump_tariff(ts(1), _cfg(night_from=0, night_to=5)) == 0.10  # non-wrapping window


def test_cost_split_by_pump_state():
    from datetime import datetime
    t0 = int(datetime(2026, 1, 15, 12, 0).timestamp())
    energy = [(t0 + 60 * i, 100 + 0.1 * i) for i in range(7)]          # 0.1 kWh per minute
    on = {'Verdichter': True, 'Betriebsart': 1}
    # an interval takes the pump sample at (or just before) its END
    pump = [(t0, on), (t0 + 60, on), (t0 + 120, on),                    # intervals ending +60, +120: heating
            (t0 + 180, {'Verdichter': True, 'Betriebsart': 2}),         # ending +180: cooling
            (t0 + 240, {'Verdichter': False, 'Betriebsart': 2}),        # ending +240: standby
            (t0 + 300, {'Verdichter': False, 'Betriebsart': 1})]        # ending +300, +360: standby
    r = appmod._heatpump_cost(energy, pump, _cfg())
    assert round(r['heating']['kwh'], 3) == 0.2 and round(r['cooling']['kwh'], 3) == 0.1
    assert round(r['standby']['kwh'], 3) == 0.3
    assert round(r['heating']['cost'], 4) == round(0.2 * 0.20, 4)
    # a hole in the meter log keeps its (real) energy as 'unknown'; a counter reset adds nothing negative
    gap = appmod._heatpump_cost([(t0, 100), (t0 + 3600, 105), (t0 + 3660, 1)], pump, _cfg())
    assert gap['unknown']['kwh'] == 5 and sum(v['kwh'] for v in gap.values()) == 5


def test_energy_series_falls_back_to_power():
    from types import SimpleNamespace as R
    flat = [R(ts=60 * i, total_energy=0.0, total_power=6000.0) for i in range(11)]   # 10 min at 6 kW
    assert round(appmod._heatpump_energy_series(flat)[-1][1], 3) == 1.0
    moving = [R(ts=60 * i, total_energy=5.0 + 0.1 * i, total_power=1.0) for i in range(3)]
    assert abs(appmod._heatpump_energy_series(moving)[-1][1] - 0.2) < 1e-9
    # one impossible counter jump (rows in different units) is replaced by the logged power, not counted
    jump = [R(ts=60 * i, total_energy=(19000.0 if i >= 5 else 300.0 + 0.1 * i), total_power=6000.0) for i in range(11)]
    assert round(appmod._heatpump_energy_series(jump)[-1][1], 3) == 0.5   # 4 real steps of 0.1 + the jump minute at 6 kW


def test_cost_settings_route(client):
    _login(client, 'hp_admin')
    assert client.get('/admin/heatpump/cost').get_json() == {'configured': False}
    good = {'meter': '', 'price_day': '0,25', 'price_night': '0.12', 'night_from': '22', 'night_to': '6'}
    client.post('/admin/heatpump/cost-settings', data=dict(good, meter='10.9.9.9'))   # unknown meter
    client.post('/admin/heatpump/cost-settings', data=dict(good, night_from='24'))    # bad hour
    with flask_app.app_context():
        assert appmod._heatpump_cost_cfg()['price_day'] == 0.0
    client.post('/admin/heatpump/cost-settings', data=good)
    with flask_app.app_context():
        cfg = appmod._heatpump_cost_cfg()
        assert (cfg['price_day'], cfg['price_night'], cfg['night_from'], cfg['night_to']) == (0.25, 0.12, 22, 6)


def test_heatpump_on_hall_plan(client, monkeypatch):
    _login(client, 'hp_admin')
    r = client.post('/admin/hall/equipment/save', json={'kind': 'heatpump', 'name': 'Термопомпа', 'x': 5, 'z': 3,
                                                        'width': 1, 'depth': 0.8, 'height': 1.4, 'elevation': 0, 'rotation': 0})
    assert r.status_code == 200 and r.get_json()['kind'] == 'heatpump' and r.get_json()['ref_id'] is None
    monkeypatch.setitem(appmod._heatpump_live, 'online', True)
    monkeypatch.setitem(appmod._heatpump_live, 'data', {'Temp. Vorlauf': 38.4, 'Temp. Ruecklauf': 36.1, 'Verdichter': True})
    live = client.get('/admin/hall/live').get_json()['heatpump']
    assert live['online'] and live['vorlauf'] == 38.4 and live['compressor'] is True and live['power'] is None
    assert 'power' in client.get('/admin/heatpump/data').get_json()


def test_convector_circuit_makes_pipe_links(client):
    _login(client, 'hp_admin')
    with flask_app.app_context():
        c = appmod.Convector(name='QA конвектор', host='10.0.0.9')
        db.session.add(c)
        db.session.commit()
        cid = c.id
    base = {'name': 'QA конвектор', 'connection_type': 'ip', 'host': '10.0.0.9', 'device_type': 'shelly_gen1', 'relay_channel': '0'}
    assert client.get(f'/admin/convectors/{cid}/edit').status_code == 200

    def pipes():
        with flask_app.app_context():
            return sorted(l['kind'] for l in appmod._hall_links() if l['a'] == ['heatpump', None] and l['b'] == ['convector', cid])

    assert pipes() == []
    client.post(f'/admin/convectors/{cid}/update', data=dict(base, heatpump_circuit='heating'))
    assert pipes() == ['heat']
    client.post(f'/admin/convectors/{cid}/update', data=dict(base, heatpump_circuit='both'))
    assert pipes() == ['cool', 'heat']
    client.post(f'/admin/convectors/{cid}/update', data=dict(base))                     # field absent: unchanged
    assert pipes() == ['cool', 'heat']
    client.post(f'/admin/convectors/{cid}/update', data=dict(base, heatpump_circuit='x'))   # invalid -> cleared
    assert pipes() == []


def test_cost_settings_mqtt_meter(client):
    _login(client, 'hp_admin')
    with flask_app.app_context():
        db.session.add(appmod.ShellyDevice(name='tp', host=None, mqtt_topic='shellies/tp_em3', connection_type='mqtt'))
        db.session.commit()
    data = {'meter': 'shellies/tp_em3', 'price_day': '0.17', 'price_night': '0.17', 'night_from': '22', 'night_to': '6'}
    r = client.post('/admin/heatpump/cost-settings', data=data, follow_redirects=True)
    with flask_app.app_context():
        assert appmod._heatpump_cost_cfg()['meter'] == 'shellies/tp_em3', r.get_data(as_text=True)[:3000]
