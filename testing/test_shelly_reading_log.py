"""
pytest regression test for ShellyReadingLog: admin_power_data() (the JSON
feed /admin/power.html polls every 2s) should write one row per online
device on every tick, with `ts` stored as unix seconds (int), not a
DateTime/calendar string - see the model's docstring in app.py for why
(reuses the existing poll route instead of a dedicated background job).

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
from werkzeug.security import generate_password_hash

import app as appmod
from app import app as flask_app, db, User, ShellyDevice, ShellyReadingLog, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        db.session.add(admin)
        db.session.add(ShellyDevice(name='QA meter', host='10.0.0.9'))
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def admin_client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c


def test_poll_tick_logs_a_row_with_unix_ts(app, admin_client, monkeypatch):
    monkeypatch.setattr(appmod, 'shelly_fleet_snapshot', lambda devices: [{
        'name': 'QA meter', 'host': '10.0.0.9', 'online': True, 'error': None,
        'channels': [], 'total_power': 123.4, 'total_energy': 5.6,
        'temperature': 30.0, 'rssi': -50,
    }])

    before = int(time.time())
    resp = admin_client.get('/admin/power/data')
    after = int(time.time())
    assert resp.status_code == 200

    with app.app_context():
        rows = ShellyReadingLog.query.all()
        assert len(rows) == 1
        row = rows[0]
        assert row.host == '10.0.0.9'
        assert isinstance(row.ts, int)
        assert before <= row.ts <= after
        assert row.total_power == 123.4
        assert row.total_energy == 5.6


def test_offline_device_is_not_logged(app, admin_client, monkeypatch):
    monkeypatch.setattr(appmod, 'shelly_fleet_snapshot', lambda devices: [{
        'name': 'QA meter', 'host': '10.0.0.9', 'online': False, 'error': 'timeout',
        'channels': [], 'total_power': 0.0, 'total_energy': 0.0,
        'temperature': None, 'rssi': None,
    }])

    resp = admin_client.get('/admin/power/data')
    assert resp.status_code == 200

    with app.app_context():
        assert ShellyReadingLog.query.count() == 0
