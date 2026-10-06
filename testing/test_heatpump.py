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
