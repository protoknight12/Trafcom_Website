"""
pytest for drag-to-zoom on the time charts (static/js/chart_zoom.js): every page with a time chart loads the module and
opens without a 500, the energy report takes ?period=range&start_ts=&end_ts= (a zoomed interval) and the heat pump
history takes ?start_ts=&end_ts=.

Run with:
    pytest testing/test_chart_zoom.py -v
"""
import json
import socket

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, ShellyDevice, ShellyReadingLog, HeatPumpReading, limiter

T0 = 1_700_000_000
PAGES = ('/admin/power', '/admin/power?host=10.0.0.5', '/admin/heatpump', '/admin/hvac', '/admin/temperature-sensors',
         '/admin/solar-roof', '/admin/device-log?kind=shelly&id={sh}')


@pytest.fixture
def client(monkeypatch):
    def no_net(*a, **k):
        raise OSError('network blocked in tests')
    monkeypatch.setattr(socket.socket, 'connect', no_net)
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        if not User.query.filter_by(username='cz_admin').first():
            db.session.add(User(username='cz_admin', password=generate_password_hash('x123456'), role='admin'))
        if not ShellyDevice.query.filter_by(host='10.0.0.5').first():
            db.session.add(ShellyDevice(name='Мерач', host='10.0.0.5', connection_type='ip'))
            for i in range(120):      # two hours, one row a minute
                db.session.add(ShellyReadingLog(host='10.0.0.5', ts=T0 + 60 * i, total_power=100.0 + i, total_energy=5.0 + i / 100,
                                                channels_json=json.dumps([{'label': 'Фаза A', 'act_power': 50.0}])))
                db.session.add(HeatPumpReading(ts=T0 + 60 * i, data_json=json.dumps({'Temp. Aussen': 10.0 + i / 10})))
        db.session.commit()
        db.session.remove()
    c = flask_app.test_client()
    c.post('/login', data={'username': 'cz_admin', 'password': 'x123456'})
    return c


def test_chart_pages_load_chart_zoom(client):
    with flask_app.app_context():
        sh = ShellyDevice.query.filter_by(host='10.0.0.5').first().id
    for url in PAGES:
        r = client.get(url.format(sh=sh))
        assert r.status_code == 200, (url, r.status_code)
        assert 'js/chart_zoom.js' in r.get_data(as_text=True), url


def test_chart_pages_wire_the_module():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for f in ('static/js/energy_panel.js', 'templates/admin_heatpump.html', 'templates/admin_hvac.html',
              'templates/admin_power.html', 'templates/admin_solar_roof.html'):
        assert 'ChartZoom.attach(' in (root / f).read_text(encoding='utf-8'), f


def test_energy_report_range(client):
    a, b = T0 + 30 * 60, T0 + 60 * 60
    j = client.get(f'/admin/energy-report?key=10.0.0.5&period=range&start_ts={a}&end_ts={b}').get_json()
    assert j['since'] == a and j['until'] == b
    ts = [p[0] for p in j['series']['power']]
    assert ts and min(ts) >= a and max(ts) <= b and len(ts) == 31
    assert client.get(f'/admin/energy-report?key=10.0.0.5&period=range&start_ts={b}&end_ts={a}').get_json()['error']
    assert client.get('/admin/energy-report?key=10.0.0.5&period=range').get_json()['error']


def test_heatpump_history_range(client):
    a, b = T0 + 10 * 60, T0 + 20 * 60
    j = client.get(f'/admin/heatpump/history?start_ts={a}&end_ts={b}').get_json()
    ts = [p[0] for p in j['Temp. Aussen']]
    assert ts == [T0 + 60 * i for i in range(10, 21)]
    # without a range: the last ?hours= as before (the seeded rows are long past)
    assert client.get('/admin/heatpump/history?hours=6').get_json()['Temp. Aussen'] == []
