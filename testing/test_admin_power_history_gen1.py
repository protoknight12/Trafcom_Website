"""
pytest regression test: /admin/power/history for a Gen1 meter (cached in
_shelly_gen_cache, e.g. from the live dashboard's earlier poll) must be
served from ShellyReadingLog (our own poll-tick log) via
_aggregate_local_shelly_log(), instead of calling shelly_history() - which
only speaks Gen2's /emdata/ API and 404s against Gen1 firmware. Also asserts
shelly_history() is never even called for a known-Gen1 host, i.e. the check
happens before the network round trip, not just around it.

Run with:
    pytest testing/test_admin_power_history_gen1.py -v
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
        db.session.add(ShellyDevice(name='QA Gen1 meter', host='10.0.0.9'))
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()
        appmod._shelly_gen_cache.clear()


@pytest.fixture
def admin_client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c


def test_gen1_host_is_served_from_the_local_log_without_hitting_the_network(app, admin_client, monkeypatch):
    appmod._shelly_gen_cache['10.0.0.9'] = 'gen1'

    def boom(*a, **kw):
        raise AssertionError('shelly_history() must not be called for a known-Gen1 host')
    monkeypatch.setattr(appmod, 'shelly_history', boom)

    with app.app_context():
        db.session.add_all([
            ShellyReadingLog(host='10.0.0.9', ts=1000, total_power=100.0, total_energy=5.0),
            ShellyReadingLog(host='10.0.0.9', ts=1500, total_power=120.0, total_energy=5.2),
            # outside the requested window - must not be counted
            ShellyReadingLog(host='10.0.0.9', ts=9999, total_power=999.0, total_energy=99.0),
        ])
        db.session.commit()

    resp = admin_client.get('/admin/power/history?host=10.0.0.9&start_ts=1000&end_ts=2000')
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload['count'] == 2
    assert payload['energy_kwh'] == 0.2  # 5.2 - 5.0
    assert payload['channels'] == {}


def test_gen1_host_with_no_logged_rows_returns_empty_not_an_error(app, admin_client, monkeypatch):
    appmod._shelly_gen_cache['10.0.0.9'] = 'gen1'
    monkeypatch.setattr(appmod, 'shelly_history', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('unused')))

    resp = admin_client.get('/admin/power/history?host=10.0.0.9&start_ts=1000&end_ts=2000')
    assert resp.status_code == 200
    assert resp.get_json() == {'count': 0, 'energy_kwh': 0.0, 'channels': {}}


def test_unknown_generation_host_still_attempts_the_live_fetch(app, admin_client, monkeypatch):
    # host never polled yet -> not in _shelly_gen_cache -> must not be
    # assumed Gen1, the normal (Gen2-first) path still runs
    called = []
    monkeypatch.setattr(appmod, 'shelly_history', lambda *a, **kw: called.append(1) or [])

    resp = admin_client.get('/admin/power/history?host=10.0.0.9&start_ts=1000&end_ts=2000')
    assert resp.status_code == 200
    assert called == [1]
