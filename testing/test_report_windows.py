"""
pytest guard for the cost / energy report endpoints (/admin/heatpump/cost, /admin/energy-report and the grid-share
helper behind them):

* the numbers they return for a fixed dataset must not change when the queries are slimmed down (column tuples,
  streaming) - EXPECTED below was computed with the code as it was BEFORE that change;
* a custom window longer than COST_WINDOW_MAX_DAYS is shortened (keeping its end) and the JSON carries a Bulgarian notice.

Run with:
    python -m pytest testing/test_report_windows.py -q -p no:cacheprovider
"""
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timedelta

# EXPECTED was captured in Europe/Sofia and the reports bucket by local time (day windows, night tariff), so pin the
# zone: on a UTC machine (GitHub Actions) every timestamp came out 2 h off. Windows has no tzset - run it there in Sofia time.
os.environ['TZ'] = 'Europe/Sofia'
if hasattr(time, 'tzset'):
    time.tzset()

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

import app as app_module
from app import (app as flask_app, db, limiter, User, EditableText, ShellyDevice, ShellyReadingLog, ModbusDevice,
                 SolisReadingLog, HeatPumpReading, TemperatureSensor, TemperatureReading)

# local midnight: a custom one-day window from/to = this date covers [BASE, BASE + 24h]
BASE = int(datetime(2026, 3, 1).timestamp())
DAY = '2026-03-01'
QS = f'period=custom&from={DAY}&to={DAY}'
N_SHELLY = 1300      # > 1200 rows -> the chart thinning keeps every 2nd row
N_SOLIS = 1300


def _seed():
    for k, v in (('meter', '10.0.0.5'), ('price_day', '0.2'), ('price_night', '0.2'), ('night_from', '22'), ('night_to', '6')):
        db.session.add(EditableText(key='settings.heatpump_' + k, content=v))
    db.session.add(ShellyDevice(name='Помпа', host='10.0.0.5'))
    # Shelly meter log: a counter that moves 0.02 kWh a minute, a hole of 11 minutes, a counter reset, a row without channels
    rows = []
    for i in range(N_SHELLY):
        if 20 <= i < 31:
            continue
        energy = i * 0.02 if i < 700 else (i - 700) * 0.02
        ch = None if i == 5 else json.dumps([
            {'label': f'Phase {p}', 'voltage': 230 + p + (i % 3), 'current': 1.5 + p + (i % 5) / 10, 'act_power': 300 + 10 * p + i % 17,
             'aprt_power': 320 + 10 * p + i % 17, 'pf': 0.9 + p / 100, 'freq': 50 + (i % 4) / 10} for p in range(3)])
        rows.append(ShellyReadingLog(host='10.0.0.5', ts=BASE + 60 * i, total_power=None if i == 7 else 1000 + 10 * (i % 40),
                                     total_energy=energy, channels_json=ch))
    db.session.add_all(rows)
    # heat pump state, one row a minute for the first 700 minutes: compressor cycles, cooling in the middle
    db.session.add_all(HeatPumpReading(ts=BASE + 60 * i, data_json=json.dumps(
        {'Verdichter': (i // 15) % 2 == 0, 'Betriebsart': 2 if 300 <= i < 500 else 1, 'Temp. Aussen': 5.5}))
        for i in range(700))
    # two Solis inverters (id 1 carries the grid CT), a virtual grid meter on inverter 1
    inv1 = ModbusDevice(name='Инвертор 1', host='10.0.1.1', port=502, device_type='solis_s6')
    inv2 = ModbusDevice(name='Инвертор 2', host='10.0.1.2', port=502, device_type='solis_s6')
    db.session.add_all([inv1, inv2])
    db.session.flush()
    db.session.add(ModbusDevice(name='Мрежа', host='10.0.1.3', port=502, device_type='solis_grid_meter', source_device_id=inv1.id))
    for i in range(N_SOLIS):
        imp = i % 7 < 4
        snap = {'ac': {'voltage_a': 231, 'voltage_b': 232, 'voltage_c': 233, 'current_a': 1 + i % 3, 'current_b': 2, 'current_c': 3,
                       'active_power': 2000 + i, 'apparent_power': 2100 + i, 'reactive_power': 50, 'power_factor': 0.97, 'frequency': 50.01},
                'meter_3p': {'active_power': (-1 if imp else 1) * (800 + 3 * (i % 50)), 'direction': 'import' if imp else 'export', 'voltage_a': 230, 'voltage_b': 231,
                             'voltage_c': 232, 'current_a': 1.1, 'current_b': 1.2, 'current_c': 1.3, 'active_power_a': 250, 'active_power_b': 260,
                             'active_power_c': 270, 'apparent_power': 900, 'reactive_power': 10, 'power_factor': 0.95, 'frequency': 49.98},
                'battery': {'soc': 40 + i % 30, 'power': 500 - i % 40, 'voltage': 51.2, 'current': 9.8, 'temperature': 25.5},
                'battery_groups': [{'soc': 1}, {'soc': 41 + i % 30, 'power': 300, 'voltage': 51.0, 'current': 5.9}]}
        db.session.add(SolisReadingLog(device_id=inv1.id, ts=BASE + 60 * i, ac_power=None if i == 3 else 2000 + i, pv_power=1500 + i % 100,
                                       temperature=40 + i % 5, snapshot_json=json.dumps(snap)))
        if i < 60:
            db.session.add(SolisReadingLog(device_id=inv2.id, ts=BASE + 60 * i + 20, ac_power=1000 + i, pv_power=900 + i,
                                           temperature=38, snapshot_json='not json{' if i == 10 else json.dumps(snap)))
    sensor = TemperatureSensor(name='Датчик', mqtt_topic='shellies/ht1')
    db.session.add(sensor)
    db.session.flush()
    db.session.add_all(TemperatureReading(sensor_id=sensor.id, ts=BASE + 60 * i, temperature=20 + i % 9 / 2,
                                          humidity=None if i == 4 else 40 + i % 11, battery=90 - i // 50) for i in range(1300))


@pytest.fixture(scope='module')
def client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with flask_app.app_context():
        db.create_all()
        db.session.add(User(username='rw_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        _seed()
        db.session.commit()
    limiter.reset()
    c = flask_app.test_client()
    c.post('/login', data={'username': 'rw_admin', 'password': 'irrelevant123'})
    yield c
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


def _digest(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


ENDPOINTS = {
    'heatpump_cost': f'/admin/heatpump/cost?{QS}',
    'shelly_report': f'/admin/energy-report?key=10.0.0.5&{QS}',
    'solis_report': f'/admin/energy-report?key=10.0.1.1:502&{QS}',
    'solis_report_bad_row': f'/admin/energy-report?key=10.0.1.2:502&{QS}',
    'grid_report': f'/admin/energy-report?key=10.0.1.3:502&{QS}',
    'battery_report': f'/admin/energy-report?key=battery:1&{QS}',
    'sensor_report': f'/admin/energy-report?key=sensor:1&{QS}',
}


def _fetch(client, name):
    res = client.get(ENDPOINTS[name])
    assert res.status_code == 200, (name, res.status_code)
    return res.get_json()


# Computed with the pre-change code (whole ORM rows, no streaming) on the dataset above.
EXPECTED = {'battery_report': {'cards': [], 'digest': '1867fa2daaeefe8c286a0328ba886fca1cae090e', 'points': 650},
 'grid_report': {'cards': [[1.6268100000000045, 8.134049999999991], [None, 5.393058333333325]],
                 'digest': '6881dcefb320a0bbdfe035a23302523b9718fa30',
                 'points': 650},
 'grid_share': {'digest': '17e75979956cb1efa8ca35e4c581266655b5d2bb',
                'first': [[1772316000, 0.2857142857142857], [1772316060, 0.21109358569926392], [1772316120, 0.21160409556313994]],
                'last': [[1772393820, 0.2220386974988202], [1772393880, 0.22253653936822254], [1772393940, 0.0]],
                'n': 1300},
 'heatpump_cost': {'categories': {'cooling': {'cost': 0.42, 'kwh': 2.1},
                                  'grid': {'cost': 0.74, 'kwh': 3.686},
                                  'heating': {'cost': 1.0, 'kwh': 5.02},
                                  'solar': {'cost': 4.45, 'kwh': 22.274},
                                  'src_unknown': {'cost': 0.0, 'kwh': 0.0},
                                  'standby': {'cost': 1.34, 'kwh': 6.7},
                                  'unknown': {'cost': 2.43, 'kwh': 12.14}},
                   'first_data': 1772316000,
                   'since': 1772316000,
                   'until': 1772402400},
 'sensor_report': {'cards': [], 'digest': 'af03b0770bdb8042c503bbaf8d45cae9e5a87c1a', 'points': 650},
 'shelly_report': {'cost': {'grid': {'cost': 0.7372095889003122, 'kwh': 3.6860479445015533},
                            'solar': {'cost': 4.454790411099649, 'kwh': 22.273952055498206},
                            'src_unknown': {'cost': 0.0, 'kwh': 0.0},
                            'total': {'cost': 5.191999999999906, 'kwh': 25.959999999999777}},
                   'digest': '2f194d6975add278106d170c30e9a5f243c04ad9',
                   'points': 645},
 'solis_report': {'cards': [[6.709334999999996, 33.546675]], 'digest': '9431ef339052d6f977f4c2cb9403619ef9243412', 'points': 650},
 'solis_report_bad_row': {'cards': [[0.18280166666666667, 0.914008333333333]],
                          'digest': '6625fcf940f6be5ffe8e164f99d080f1b44b142b',
                          'points': 60}}


def _summary(name, j):
    """Readable numbers + a digest of the whole response."""
    if name == 'heatpump_cost':
        return {'categories': j['categories'], 'since': j['since'], 'until': j['until'], 'first_data': j['first_data']}
    if 'cost' in j and j['cost']:
        return {'cost': j['cost'], 'points': len(j['series']['power']), 'digest': _digest(j)}
    return {'cards': [[c['cost'], c['kwh']] for c in j.get('cards') or []], 'points': len(next(iter(j['series'].values()))), 'digest': _digest(j)}


@pytest.mark.parametrize('name', list(ENDPOINTS))
def test_report_numbers_unchanged(client, name):
    got = _summary(name, _fetch(client, name))
    if os.environ.get('CAPTURE_EXPECTED'):
        print('CAPTURE', name, json.dumps(got, sort_keys=True))
        return
    assert got == EXPECTED[name]


def test_grid_share_rows_unchanged(client):
    with flask_app.test_request_context('/'):
        rows = app_module._grid_share_rows(BASE)
    got = {'n': len(rows), 'digest': _digest(rows), 'first': [list(r) for r in rows[:3]], 'last': [list(r) for r in rows[-3:]]}
    if os.environ.get('CAPTURE_EXPECTED'):
        print('CAPTURE grid_share', json.dumps(got, sort_keys=True))
        return
    assert got == EXPECTED['grid_share']


def test_long_custom_window_is_clamped_with_notice(client):
    j = client.get('/admin/heatpump/cost?period=custom&from=2020-01-01&to=2026-03-01').get_json()
    assert app_module.COST_WINDOW_MAX_DAYS == 366
    assert j['until'] == BASE + 86400                                         # the end is kept
    assert abs((j['until'] - j['since']) - 366 * 86400) <= 3600               # (a DST change inside the year moves it by an hour)
    assert j['notice'] == 'Избраният период е по-дълъг от една година и е съкратен до последната година.'
    for key in ('10.0.0.5', '10.0.1.1:502', 'battery:1', 'sensor:1'):         # every report kind carries it
        r = client.get(f'/admin/energy-report?key={key}&period=custom&from=2020-01-01&to=2026-03-01').get_json()
        assert r['notice'] and abs((r['until'] - r['since']) - 366 * 86400) <= 3600, key


def test_a_window_of_exactly_the_maximum_is_not_clamped(client):
    # 2025-03-02 .. 2026-03-01, both days included = 365 days; 2025-02-28 .. 2026-03-01 = 367 days -> clamped
    ok = client.get('/admin/heatpump/cost?period=custom&from=2025-03-01&to=2026-03-01').get_json()   # 366 + 1 = 366 days of window? see below
    span = (datetime(2026, 3, 1) + timedelta(days=1) - datetime(2025, 3, 1)).days
    assert ('notice' in ok) == (span > app_module.COST_WINDOW_MAX_DAYS)
    short = client.get('/admin/heatpump/cost?period=custom&from=2025-03-02&to=2026-03-01').get_json()
    assert 'notice' not in short and short['since'] == int(datetime(2025, 3, 2).timestamp())
