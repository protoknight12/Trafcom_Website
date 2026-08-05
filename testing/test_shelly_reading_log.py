"""
pytest regression test for ShellyReadingLog / _shelly_history_poll_tick():
the background poller (see start_shelly_history_poller()) should write one
row per online device on every tick, with `ts` stored as unix seconds (int),
not a DateTime/calendar string. Tests _shelly_history_poll_tick() directly -
the standalone function the real poller thread calls once a minute - rather
than starting the actual thread/sleep loop or going through a route (the
old admin_power_data()-piggybacked logging was replaced by this real
always-on poller; see the model's docstring in app.py for why).

Run with:
    pytest testing/test_shelly_reading_log.py -v
"""
import atexit
import os
import tempfile
import time

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

import app as appmod
from app import app as flask_app, db, ShellyDevice, ShellyReadingLog


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with flask_app.app_context():
        db.create_all()
        db.session.add(ShellyDevice(name='QA meter', host='10.0.0.9'))
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


def test_poll_tick_logs_a_row_with_unix_ts(app, monkeypatch):
    monkeypatch.setattr(appmod, 'shelly_fleet_snapshot', lambda devices: [{
        'name': 'QA meter', 'host': '10.0.0.9', 'online': True, 'error': None,
        'channels': [{'label': 'Фаза A', 'voltage': 230.0, 'current': 1.0,
                      'act_power': 200.0, 'aprt_power': 230.0, 'pf': 0.9, 'freq': 50.0}],
        'total_power': 123.4, 'total_energy': 5.6,
        'temperature': 30.0, 'rssi': -50,
    }])

    with app.app_context():
        before = int(time.time())
        appmod._shelly_history_poll_tick()
        after = int(time.time())

        rows = ShellyReadingLog.query.all()
        assert len(rows) == 1
        row = rows[0]
        assert row.host == '10.0.0.9'
        assert isinstance(row.ts, int)
        assert before <= row.ts <= after
        assert row.total_power == 123.4
        assert row.total_energy == 5.6
        import json as json_mod
        stored_channels = json_mod.loads(row.channels_json)
        assert stored_channels[0]['voltage'] == 230.0


def test_offline_device_is_not_logged(app, monkeypatch):
    monkeypatch.setattr(appmod, 'shelly_fleet_snapshot', lambda devices: [{
        'name': 'QA meter', 'host': '10.0.0.9', 'online': False, 'error': 'timeout',
        'channels': [], 'total_power': 0.0, 'total_energy': 0.0,
        'temperature': None, 'rssi': None,
    }])

    with app.app_context():
        appmod._shelly_history_poll_tick()
        assert ShellyReadingLog.query.count() == 0


def test_no_devices_configured_is_a_noop(app, monkeypatch):
    with app.app_context():
        ShellyDevice.query.delete()
        db.session.commit()

        def boom(devices):
            raise AssertionError('shelly_fleet_snapshot() must not be called with no devices')
        monkeypatch.setattr(appmod, 'shelly_fleet_snapshot', boom)

        appmod._shelly_history_poll_tick()  # must not raise
        assert ShellyReadingLog.query.count() == 0


def test_start_shelly_history_poller_is_idempotent(app, monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.threading, 'Thread',
                         lambda **kw: calls.append(kw) or type('T', (), {'start': lambda self: None})())
    appmod._shelly_poller_started = False
    try:
        appmod.start_shelly_history_poller()
        appmod.start_shelly_history_poller()
        assert len(calls) == 1
    finally:
        appmod._shelly_poller_started = False
