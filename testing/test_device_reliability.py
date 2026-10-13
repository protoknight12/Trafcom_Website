"""
pytest for the reliability layer around plant hardware (Shelly/MQTT/Modbus/
heat pump/detector) - no device is ever contacted, every read is a fake:

    _live_cached()            single flight (N concurrent callers -> one read), re-read after the TTL, a stalled read
                              answers waiters with the last good reading, a failure is not reused past the TTL
    _mqtt_snapshot() etc.     state older than MQTT_STALE_AFTER / a broker disconnect -> offline
    _modbus_gateway()         a held gateway lock -> the offline shape after MODBUS_LOCK_TIMEOUT, never a hang
    /health                   200 + ok, "degraded" with a stale poller, 503 without the DB, nothing but names and ages
    _poller_run_tick()        a tick that raises is logged (rate-limited) and does not raise
    _detection_loop_once()    its failure path no longer raises outside the app context (that killed the thread)
    _mqtt_publish()           reports not-sent when disconnected; the convector toggle then says so

Run with:
    pytest testing/test_device_reliability.py -v
"""
import atexit
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

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
from flask import has_app_context
from werkzeug.security import generate_password_hash

import app as appmod
from app import app as flask_app, db, User, Convector, ActivityLog, limiter


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Every test starts with empty caches / poller bookkeeping and the production timings."""
    for d in (appmod._live_cache, appmod._poller_seen, appmod._poller_last_logged, appmod._mqtt_state, appmod._mqtt_conv_state,
              appmod._mqtt_temp_state, appmod._modbus_lock_since):
        d.clear()
    monkeypatch.setattr(appmod, 'LIVE_CACHE_TTL', 2.0)
    monkeypatch.setattr(appmod, 'LIVE_CACHE_WAIT', 2.0)
    monkeypatch.setattr(appmod, 'LIVE_CACHE_STALE_MAX', 30.0)
    monkeypatch.setattr(appmod, 'MODBUS_LOCK_TIMEOUT', 2.0)
    monkeypatch.setattr(appmod, '_mqtt_client_instance', None)
    yield


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        if not User.query.filter_by(username='rel_admin').first():
            db.session.add(User(username='rel_admin', password=generate_password_hash('x123456'), role='admin'))
        db.session.commit()
        db.session.remove()
    return flask_app.test_client()


BUSY = {'online': False, 'error': 'busy'}


def _in_threads(n, fn):
    out, threads = [None] * n, []
    for i in range(n):
        t = threading.Thread(target=lambda i=i: out.__setitem__(i, fn()))
        threads.append(t)
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads), 'a caller hung'
    return out


# ----------------- live snapshot cache -----------------

def test_concurrent_callers_cost_one_read():
    calls = []

    def read():
        calls.append(1)
        time.sleep(0.3)
        return {'online': True, 'total_power': 42.0, 'channels': []}

    out = _in_threads(8, lambda: appmod._live_cached('k', read, lambda: BUSY))
    assert len(calls) == 1
    assert all(o == {'online': True, 'total_power': 42.0, 'channels': []} for o in out)
    # a copy each: one caller's own keys ('kind', 'machine' ...) must not leak into the next caller's snapshot
    out[0]['kind'] = 'shelly'
    out[0]['channels'].append('x')
    assert appmod._live_cached('k', read, lambda: BUSY) == {'online': True, 'total_power': 42.0, 'channels': []}
    assert len(calls) == 1


def test_read_again_after_the_ttl(monkeypatch):
    monkeypatch.setattr(appmod, 'LIVE_CACHE_TTL', 0.2)
    calls = []

    def read():
        calls.append(1)
        return {'online': True, 'n': len(calls)}

    assert appmod._live_cached('k', read, lambda: BUSY)['n'] == 1
    assert appmod._live_cached('k', read, lambda: BUSY)['n'] == 1
    time.sleep(0.3)
    assert appmod._live_cached('k', read, lambda: BUSY)['n'] == 2
    assert len(calls) == 2


def test_ttl_zero_switches_the_cache_off(monkeypatch):
    monkeypatch.setattr(appmod, 'LIVE_CACHE_TTL', 0)
    calls = []
    for _ in range(3):
        appmod._live_cached('k', lambda: calls.append(1) or {'online': True}, lambda: BUSY)
    assert len(calls) == 3 and not appmod._live_cache


def test_stalled_read_does_not_hold_the_other_callers(monkeypatch):
    """A read that hangs: the others wait LIVE_CACHE_WAIT at most (counted from the read's start), then get the last good
    reading - or the offline shape when there is none recent enough."""
    monkeypatch.setattr(appmod, 'LIVE_CACHE_TTL', 0.1)
    monkeypatch.setattr(appmod, 'LIVE_CACHE_WAIT', 0.3)
    release = threading.Event()
    assert appmod._live_cached('k', lambda: {'online': True, 'n': 1}, lambda: BUSY)['n'] == 1
    time.sleep(0.15)                                    # the first reading is past its TTL now

    def stalled():
        release.wait(10)
        return {'online': True, 'n': 2}

    reader = threading.Thread(target=lambda: appmod._live_cached('k', stalled, lambda: BUSY))
    reader.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    assert appmod._live_cached('k', stalled, lambda: BUSY) == {'online': True, 'n': 1}     # the last good reading
    assert time.monotonic() - t0 < 1.0
    time.sleep(0.3)                                     # the read is older than LIVE_CACHE_WAIT: nobody queues behind it any more
    t0 = time.monotonic()
    appmod._live_cached('k', stalled, lambda: BUSY)
    assert time.monotonic() - t0 < 0.1
    monkeypatch.setattr(appmod, 'LIVE_CACHE_STALE_MAX', 0.1)                               # ... and too old to pass off as live
    assert appmod._live_cached('k', stalled, lambda: BUSY) == BUSY
    release.set()
    reader.join(5)
    assert appmod._live_cached('k', stalled, lambda: BUSY)['n'] == 2


def test_failure_is_not_reused_past_the_ttl(monkeypatch):
    monkeypatch.setattr(appmod, 'LIVE_CACHE_TTL', 0.1)
    assert appmod._live_cached('k', lambda: {'online': False, 'error': 'timeout'}, lambda: BUSY)['error'] == 'timeout'
    time.sleep(0.15)

    def busy_gateway():
        raise appmod.DeviceBusy('busy')
    # the gateway is busy, the only reading is an old failure: not served again, however large LIVE_CACHE_STALE_MAX is
    assert appmod._live_cached('k', busy_gateway, lambda: BUSY) == BUSY


def test_shelly_snapshot_is_cached_per_host_and_keeps_the_callers_name(monkeypatch):
    calls = []

    def fake_status(host, timeout=None):
        calls.append(host)
        return {'em:0': {'total_act_power': 100.0}, 'emdata:0': {'total_act': 5000.0}}

    monkeypatch.setattr(appmod, '_shelly_get_status', fake_status)
    snaps = appmod.shelly_fleet_snapshot([('A', '10.9.9.1', None, 'ip'), ('B', '10.9.9.2', None, 'ip')])
    again = appmod.shelly_device_snapshot('A renamed', '10.9.9.1')
    assert sorted(calls) == ['10.9.9.1', '10.9.9.2']                # the second call for .1 was served from the cache
    assert [s['name'] for s in snaps] == ['A', 'B'] and again['name'] == 'A renamed'
    assert again['online'] and again['total_power'] == 100.0 and again['total_energy'] == 5.0
    assert set(again) == {'name', 'host', 'online', 'error', 'channels', 'total_power', 'total_energy', 'temperature', 'rssi'}


# ----------------- MQTT staleness -----------------

def _power_state(age_s):
    return {'online': True, 'last_seen': datetime.utcnow() - timedelta(seconds=age_s),
            'channels': {0: {'power': 500.0, 'voltage': 230.0, 'current': 2.2, 'total': 1000.0}}}


def test_mqtt_power_older_than_the_threshold_is_offline():
    appmod._mqtt_state['m/fresh'] = _power_state(10)
    appmod._mqtt_state['m/frozen'] = _power_state(appmod.MQTT_STALE_AFTER + 5)
    fresh, frozen = appmod._mqtt_snapshot('F', 'm/fresh'), appmod._mqtt_snapshot('Z', 'm/frozen')
    assert fresh['online'] and fresh['total_power'] == 500.0
    assert not frozen['online'] and frozen['total_power'] == 0.0 and frozen['channels'] == []
    assert set(fresh) == set(frozen)


def test_mqtt_disconnect_marks_everything_offline():
    appmod._mqtt_state['m/a'] = _power_state(1)
    appmod._mqtt_conv_state['c/a'] = {'online': True, 'last_seen': datetime.utcnow(), 'channels': {0: {'is_on': True, 'power_w': 900.0}}}
    appmod._mqtt_temp_state['t/a'] = {'online': True, 'last_seen': datetime.utcnow(), 'temperature': 21.0, 'humidity': 40.0, 'battery': 90}
    conv = SimpleNamespace(mqtt_topic='c/a', relay_channel=0, device_type='shelly_gen1')
    assert appmod._mqtt_convector_snapshot(conv)['is_on'] is True
    appmod._mqtt_mark_all_offline()
    assert not appmod._mqtt_snapshot('A', 'm/a')['online']
    assert appmod._mqtt_convector_snapshot(conv) == {'online': False, 'is_on': None, 'power_w': None, 'error': None}
    assert appmod._mqtt_temp_state['t/a']['online'] is False and appmod._mqtt_temp_state['t/a']['temperature'] == 21.0
    appmod._handle_mqtt_power_message('m/a', 'emeter/0/power', '510.0')         # its next reading brings it back
    assert appmod._mqtt_snapshot('A', 'm/a')['online']


def test_mqtt_convector_staleness_is_per_generation():
    """Gen1 republishes periodically -> a silent one is offline; Gen2 only publishes on a change -> silence is not a fault."""
    old = datetime.utcnow() - timedelta(seconds=appmod.MQTT_STALE_AFTER + 5)
    appmod._mqtt_conv_state['c/x'] = {'online': True, 'last_seen': old, 'channels': {0: {'is_on': True, 'power_w': None}}}
    gen1 = SimpleNamespace(mqtt_topic='c/x', relay_channel=0, device_type='shelly_gen1')
    gen2 = SimpleNamespace(mqtt_topic='c/x', relay_channel=0, device_type='shelly_gen2')
    assert appmod._mqtt_convector_snapshot(gen1)['online'] is False
    assert appmod._mqtt_convector_snapshot(gen2) == {'online': True, 'is_on': True, 'power_w': None, 'error': None}


# ----------------- Modbus gateway lock -----------------

def _no_modbus(*a, **k):
    raise AssertionError('a Modbus connection was opened although the gateway lock is held')


def test_held_gateway_lock_gives_the_offline_shape_without_hanging(monkeypatch):
    monkeypatch.setattr(appmod, 'MODBUS_LOCK_TIMEOUT', 0.3)
    monkeypatch.setattr(appmod, 'ModbusTcpClient', _no_modbus)
    lock = appmod._modbus_lock_for('10.9.9.50', 502)
    dtsu = SimpleNamespace(id=None, name='Main', host='10.9.9.50', port=502, unit_id=12)
    solis = SimpleNamespace(id=None, name='Inv', host='10.9.9.50', port=502, unit_id=1)
    assert lock.acquire(timeout=1)
    try:
        t0 = time.monotonic()
        assert appmod._modbus_read_raw('10.9.9.50', 502, 12, 0x2000, 6, 'holding') == (None, appmod.MODBUS_BUSY_ERROR)
        d, s = appmod._dtsu666_snapshot(dtsu), appmod._solis_snapshot(solis)
        with pytest.raises(appmod.DeviceBusy):
            appmod._solis_read_blocks(solis, [(33029, 2)])
        assert time.monotonic() - t0 < 5
    finally:
        lock.release()
    assert d == {'name': 'Main', 'host': '10.9.9.50:502', 'online': False, 'error': appmod.MODBUS_BUSY_ERROR, 'channels': [],
                 'total_power': 0.0, 'total_energy': 0.0, 'temperature': None, 'rssi': None}
    assert s['online'] is False and s['name'] == 'Inv' and s['error'] == appmod.LIVE_BUSY_ERROR
    # exactly the keys of _solis_snapshot()'s own offline dict - what every caller already handles
    assert set(s) == {'name', 'host', 'online', 'error', 'channels', 'total_power', 'total_energy', 'temperature', 'pv', 'ac',
                      'battery', 'load', 'meter', 'generator', 'battery_groups', 'meter_3p'}
    assert all(s[k] is None for k in ('pv', 'ac', 'battery', 'load', 'meter', 'generator', 'battery_groups', 'meter_3p'))
    # downstream: the battery page / virtual grid meter read a busy inverter exactly like an offline one
    stack = SimpleNamespace(source_type='inverter', inverter_device_id=1, bms_port='1')
    assert appmod._battery_stack_live_data(stack, s) is None
    assert appmod._solis_grid_meter_view_snapshot(SimpleNamespace(name='G', host='h', port=1), s)['online'] is False


def test_stalled_gateway_holder_turns_later_callers_away_at_once(monkeypatch):
    monkeypatch.setattr(appmod, 'MODBUS_LOCK_TIMEOUT', 0.2)
    monkeypatch.setattr(appmod, 'ModbusTcpClient', _no_modbus)
    entered, release = threading.Event(), threading.Event()

    def holder():
        with appmod._modbus_gateway('10.9.9.51', 502):
            entered.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    assert entered.wait(5)
    time.sleep(0.3)                                     # the holder is older than MODBUS_LOCK_TIMEOUT: stalled
    t0 = time.monotonic()
    assert appmod._modbus_read_raw('10.9.9.51', 502, 1, 0, 2, 'input') == (None, appmod.MODBUS_BUSY_ERROR)
    assert time.monotonic() - t0 < 0.1
    release.set()
    t.join(5)
    with appmod._modbus_gateway('10.9.9.51', 502):      # released properly: free again
        pass


def test_busy_gateway_serves_the_inverters_last_reading(monkeypatch):
    """The second inverter behind one WiFi stick is mid-read: the first keeps its last reading instead of flickering offline."""
    monkeypatch.setattr(appmod, 'LIVE_CACHE_TTL', 0.1)
    dev = SimpleNamespace(id=7, name='Inv 1', host='10.9.9.52', port=502, unit_id=1)
    good = {'name': 'Inv 1', 'device_id': 7, 'host': '10.9.9.52:502', 'online': True, 'error': None, 'total_power': 1900}
    appmod._live_cache[('solis', 7, '10.9.9.52', 502, 1)] = {'lock': threading.Lock(), 'hit': (time.monotonic() - 5, good), 'started': None}
    monkeypatch.setattr(appmod, 'MODBUS_LOCK_TIMEOUT', 0.1)
    monkeypatch.setattr(appmod, 'ModbusTcpClient', _no_modbus)
    lock = appmod._modbus_lock_for('10.9.9.52', 502)
    assert lock.acquire(timeout=1)
    try:
        assert appmod._solis_snapshot(dev) == good
    finally:
        lock.release()


# ----------------- /health -----------------

def test_health_ok_degraded_and_leaks_nothing(client):
    r = client.get('/health')                           # no login
    assert r.status_code == 200
    assert r.get_json() == {'status': 'ok', 'db': 'ok', 'pollers': {n: 'not_started' for n in appmod.POLLER_STALE_AFTER}}
    assert set(appmod.POLLER_STALE_AFTER) == {'shelly', 'solis', 'mqtt', 'detector', 'heatpump'}

    appmod._poller_mark('shelly')
    appmod._poller_seen['solis'] = time.time() - appmod.POLLER_STALE_AFTER['solis'] - 30
    appmod._mqtt_state['192.168.18.72'] = _power_state(1)
    appmod._heatpump_live.update(error='Connection to 192.168.18.123:23 refused')
    r = client.get('/health')
    body = r.get_json()
    assert r.status_code == 200 and body['status'] == 'degraded' and body['db'] == 'ok'
    assert body['pollers']['shelly']['ok'] is True and body['pollers']['shelly']['age_s'] <= 2
    assert body['pollers']['solis']['ok'] is False and body['pollers']['solis']['age_s'] >= appmod.POLLER_STALE_AFTER['solis']
    assert body['pollers']['heatpump'] == 'not_started'
    text = r.get_data(as_text=True)
    assert '192.168' not in text and 'refused' not in text and 'sqlite' not in text
    assert set(body) == {'status', 'db', 'pollers'}
    assert all(p == 'not_started' or set(p) == {'age_s', 'ok'} for p in body['pollers'].values())


def test_health_is_503_without_the_database(client, monkeypatch):
    def down(*a, **k):
        raise RuntimeError('could not connect to server at 10.0.0.5')
    monkeypatch.setattr(db.session, 'execute', down)
    r = client.get('/health')
    assert r.status_code == 503 and r.get_json()['status'] == 'down' and r.get_json()['db'] == 'down'
    assert '10.0.0.5' not in r.get_data(as_text=True)


def test_health_is_not_rate_limited(client):
    assert all(client.get('/health').status_code == 200 for _ in range(320))    # the default limit is 300 per hour


# ----------------- pollers: a failing tick is logged, the loop survives -----------------

def test_failing_tick_is_logged_once_and_does_not_raise(caplog, monkeypatch):
    def boom():
        raise RuntimeError('no such column: shelly_reading_log.channels_json')

    monkeypatch.setattr(appmod, '_shelly_history_poll_tick', boom)
    assert not has_app_context()
    with caplog.at_level(logging.ERROR):
        # exactly what start_shelly_history_poller()'s loop runs each minute
        assert appmod._poller_run_tick('shelly', lambda: appmod._shelly_history_poll_tick()) is False
        assert appmod._poller_run_tick('shelly', lambda: appmod._shelly_history_poll_tick()) is False
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1                              # the second failure is inside POLLER_LOG_EVERY: not logged again
    assert errors[0].exc_info and 'no such column' in str(errors[0].exc_info[1])
    assert 'shelly' not in appmod._poller_seen           # no success recorded

    appmod._poller_last_logged['shelly'] = time.time() - appmod.POLLER_LOG_EVERY - 1
    with caplog.at_level(logging.ERROR):
        appmod._poller_run_tick('shelly', lambda: appmod._shelly_history_poll_tick())
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 2   # still failing later: logged again

    assert appmod._poller_run_tick('shelly', lambda: None) is True
    assert time.time() - appmod._poller_seen['shelly'] < 2


def test_real_shelly_tick_through_the_guard(client, monkeypatch):
    """The real tick (not a stub) through the same guard: it writes its row and counts as a success."""
    monkeypatch.setattr(appmod, 'shelly_fleet_snapshot', lambda devices: [{
        'name': 'QA', 'host': '10.9.9.9', 'online': True, 'error': None, 'channels': [], 'total_power': 12.5, 'total_energy': 1.0,
        'temperature': None, 'rssi': None}])
    with flask_app.app_context():
        appmod.ShellyReadingLog.query.delete()
        appmod.ShellyDevice.query.delete()
        db.session.add(appmod.ShellyDevice(name='QA', host='10.9.9.9'))
        db.session.commit()
        db.session.remove()
    assert appmod._poller_run_tick('shelly', lambda: appmod._shelly_history_poll_tick()) is True
    with flask_app.app_context():
        assert [r.total_power for r in appmod.ShellyReadingLog.query.filter_by(host='10.9.9.9')] == [12.5]
        db.session.remove()


def test_detector_failure_no_longer_raises_outside_the_app_context(caplog, monkeypatch):
    """The old loop called db.session.rollback() after the app context had closed -> RuntimeError out of the except block, thread dead."""
    def boom(memo):
        raise RuntimeError('camera table is gone')

    monkeypatch.setattr(appmod, '_detection_tick', boom)
    st = {'has_lock': False, 'last_clean': 0}
    assert not has_app_context()
    with caplog.at_level(logging.ERROR):
        assert appmod._detection_loop_once({}, st) is False              # returned - did not raise
        assert appmod._detection_loop_once({}, st) is False              # ... and the next pass runs too
    assert st['has_lock'] is True                                        # sqlite: no advisory lock, the tick itself was reached
    assert any('camera table is gone' in str(r.exc_info[1]) for r in caplog.records if r.exc_info)
    assert not any('application context' in r.getMessage() for r in caplog.records)

    ticks = []
    monkeypatch.setattr(appmod, '_detection_tick', lambda memo: ticks.append(1))
    monkeypatch.setattr(appmod, '_detection_cleanup', lambda: ticks.append('clean'))
    assert appmod._detection_loop_once({}, st) is True
    assert ticks == [1, 'clean'] and st['last_clean'] > 0 and 'detector' in appmod._poller_seen


# ----------------- MQTT publish / convector toggle -----------------

class _FakeMqtt:
    def __init__(self, connected, rc=0):
        self.connected, self.rc, self.sent = connected, rc, []

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload):
        self.sent.append((topic, payload))
        return SimpleNamespace(rc=self.rc)


def test_mqtt_publish_reports_whether_it_was_sent(monkeypatch):
    assert appmod._mqtt_publish('t/relay/0/command', 'on') is False          # no client at all (MQTT_BROKER_HOST unset)
    down = _FakeMqtt(connected=False)
    monkeypatch.setattr(appmod, '_mqtt_client_instance', down)
    assert appmod._mqtt_publish('t/relay/0/command', 'on') is False and down.sent == []
    refused = _FakeMqtt(connected=True, rc=appmod.mqtt_client.MQTT_ERR_NO_CONN)
    monkeypatch.setattr(appmod, '_mqtt_client_instance', refused)
    assert appmod._mqtt_publish('t/relay/0/command', 'on') is False
    up = _FakeMqtt(connected=True)
    monkeypatch.setattr(appmod, '_mqtt_client_instance', up)
    assert appmod._mqtt_publish('t/relay/0/command', 'on') is True and up.sent == [('t/relay/0/command', 'on')]


def test_convector_toggle_is_honest_when_the_command_was_not_sent(client, monkeypatch):
    with flask_app.app_context():
        conv = Convector(name='QA конвектор', connection_type='mqtt', mqtt_topic='qa/conv', device_type='shelly_gen1', relay_channel=0)
        db.session.add(conv)
        db.session.commit()
        conv_id = conv.id
        ActivityLog.query.delete()
        db.session.commit()
        db.session.remove()
    appmod._mqtt_conv_state['qa/conv'] = {'online': True, 'last_seen': datetime.utcnow(), 'channels': {0: {'is_on': False, 'power_w': None}}}
    client.post('/login', data={'username': 'rel_admin', 'password': 'x123456'})

    r = client.post(f'/admin/convectors/{conv_id}/toggle', follow_redirects=True)      # the broker is not connected
    page = r.get_data(as_text=True)
    assert 'командата не беше изпратена – няма връзка с MQTT брокера' in page
    assert 'беше включен' not in page
    with flask_app.app_context():
        actions = [a.action for a in ActivityLog.query.all()]
        assert any('командата не беше изпратена' in a for a in actions) and not any(a.startswith('Включен') for a in actions)
        ActivityLog.query.delete()
        db.session.commit()
        db.session.remove()

    up = _FakeMqtt(connected=True)                                                     # connected: unchanged behaviour
    monkeypatch.setattr(appmod, '_mqtt_client_instance', up)
    r = client.post(f'/admin/convectors/{conv_id}/toggle', follow_redirects=True)
    assert 'беше включен' in r.get_data(as_text=True) and up.sent == [('qa/conv/relay/0/command', 'on')]
    with flask_app.app_context():
        assert any(a.action.startswith('Включен конвектор') for a in ActivityLog.query.all())
        db.session.delete(db.session.get(Convector, conv_id))
        db.session.commit()
        db.session.remove()
