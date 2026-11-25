"""pytest: thermostat models, defaults, relationships and the migration's column list.

    python -m pytest testing/test_thermostats.py -q
"""
import os
import re

from app import (app, db, Thermostat, ThermostatInterval, HeatingHoliday, Convector,
                 ConvectorLog, TemperatureSensor)
import datetime


def test_models_and_migration_columns():
    with app.app_context():
        db.create_all()
        t = Thermostat(name='Цех')
        t.intervals.append(ThermostatInterval(season='heat', weekday=0, start_min=360, end_min=1080, temp=21))
        c = Convector(name='К1')
        c.thermostat = t
        db.session.add_all([t, c, HeatingHoliday(day=datetime.date(2026, 12, 25), name='Коледа')])
        db.session.commit()
        assert (t.hysteresis, t.min_cycle_min, t.lowered_heat) == (0.5, 5, 16.0)
        assert t.lowered_cool is None and t.override_temp is None and t.override_until is None
        assert t.enabled is True and t.hold is False and t.sensor_id is None
        assert t.convectors == [c] and c.thermostat_id == t.id
        assert ConvectorLog(convector_id=1, ts=1, is_on=True, source='thermostat').source == 'thermostat'
        db.session.delete(t)
        db.session.commit()
        assert ThermostatInterval.query.count() == 0

    path = os.path.join(os.path.dirname(__file__), '..', 'migration', 'migrate_add_thermostats.py')
    src = open(path, encoding='utf-8').read()
    assert re.search(r'ALTER TABLE convector ADD COLUMN IF NOT EXISTS thermostat_id', src)
    assert re.search(r'ALTER TABLE convector_log ADD COLUMN IF NOT EXISTS source', src)
    assert 'CREATE INDEX IF NOT EXISTS ix_convector_thermostat_id' in src
    assert src.index('db.create_all()') < src.index('ALTER TABLE')


def test_sensor_delete_nulls_thermostat_sensor():
    with app.app_context():
        db.create_all()
        s = TemperatureSensor(name='S', mqtt_topic='t/hvac-test')
        db.session.add(s)
        db.session.commit()
        t = Thermostat(name='T2', sensor_id=s.id)
        db.session.add(t)
        db.session.commit()
        tid, sid = t.id, s.id
    from app import User, limiter
    from werkzeug.security import generate_password_hash
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with app.app_context():
        if not User.query.filter_by(username='hvac_admin').first():
            db.session.add(User(username='hvac_admin', password=generate_password_hash('x123456'), role='admin'))
            db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'hvac_admin', 'password': 'x123456'})
    r = c.post(f'/admin/temperature-sensors/{sid}/delete')
    assert r.status_code in (302, 303)
    with app.app_context():
        assert db.session.get(Thermostat, tid).sensor_id is None


# ---------------- Task 3: the engine (tick, season, poller) ----------------
# Schedule time is the frozen local `now` passed to _thermostat_tick(); ages (sensor, ConvectorLog) are real time.time().
import time
import pytest
import app as app_module
from app import EditableText, HVAC_SEASON_KEY, MqttNotSent, APP_TZ

NOW = datetime.datetime(2026, 11, 23, 10, 0)   # local naive


@pytest.fixture
def env(monkeypatch):
    """Clean thermostat tables + fake devices: status[name], temp, age (s, None = never), sets (name, on), fail (names that raise)."""
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    from app import TemperatureReading
    for m in (ThermostatInterval, ConvectorLog, Convector, Thermostat, HeatingHoliday, TemperatureReading):
        m.query.delete()
    EditableText.query.filter_by(key=HVAC_SEASON_KEY).delete()
    db.session.commit()
    mqtt_state = {}
    monkeypatch.setattr(app_module, '_mqtt_temp_state', mqtt_state)

    class Env(dict):
        """Setting temp/age writes the REAL MQTT cache shape (last_seen = UTC naive datetime); age None = nothing received."""
        def __setitem__(self, k, v):
            super().__setitem__(k, v)
            if k in ('temp', 'age') and 'age' in self:
                mqtt_state.pop('t/hvac-tick', None)
                if self['age'] is not None:
                    mqtt_state['t/hvac-tick'] = {
                        'online': True, 'temperature': self['temp'], 'humidity': 40.0, 'battery': 90,
                        'last_seen': datetime.datetime.utcnow() - datetime.timedelta(seconds=self['age'])}

    e = Env(status={}, sets=[], fail=set())
    e['temp'] = 20.0
    e['age'] = 60

    def fake_status(conv):
        return e['status'].get(conv.name, {'online': True, 'is_on': False, 'power_w': None})

    def fake_set(conv, on):
        if conv.name in e['fail']:
            raise MqttNotSent('няма връзка с MQTT брокера')
        e['sets'].append((conv.name, on))

    monkeypatch.setattr(app_module, '_shelly_convector_status', fake_status)
    monkeypatch.setattr(app_module, '_shelly_convector_set', fake_set)
    yield e
    db.session.rollback()
    db.session.remove()
    ctx.pop()


def _season(s):
    db.session.merge(EditableText(key=HVAC_SEASON_KEY, content=s))
    db.session.commit()


def _thermo(convs=('К1',), heat=21.0, cool=24.0, sensor=True, **kw):
    s = None
    if sensor:
        s = TemperatureSensor.query.filter_by(mqtt_topic='t/hvac-tick').first() or TemperatureSensor(name='S', mqtt_topic='t/hvac-tick')
        db.session.add(s)
        db.session.flush()
    t = Thermostat(name='Цех', sensor_id=s.id if s else None, **kw)
    for wd in range(7):
        t.intervals.append(ThermostatInterval(season='heat', weekday=wd, start_min=0, end_min=1440, temp=heat))
        t.intervals.append(ThermostatInterval(season='cool', weekday=wd, start_min=0, end_min=1440, temp=cool))
    db.session.add(t)
    for c in convs:
        name, circuit = c if isinstance(c, tuple) else (c, None)
        db.session.add(Convector(name=name, thermostat=t, heatpump_circuit=circuit))
    db.session.commit()
    return t


def _logs(name):
    c = Convector.query.filter_by(name=name).one()
    return ConvectorLog.query.filter_by(convector_id=c.id).order_by(ConvectorLog.id).all()


def test_tick_heats_below_band(env):
    _season('heat')
    _thermo()
    env['temp'] = 20.0                                   # target 21, hysteresis 0.5
    app_module._thermostat_tick(NOW)
    assert env['sets'] == [('К1', True)]


def test_tick_deadband_no_command(env):
    _season('heat')
    _thermo()
    for temp, is_on in ((20.8, False), (21.4, True)):
        env['temp'] = temp
        env['status']['К1'] = {'online': True, 'is_on': is_on, 'power_w': None}
        app_module._thermostat_tick(NOW)
    assert env['sets'] == []
    env['temp'] = 21.6
    app_module._thermostat_tick(NOW)
    assert env['sets'] == [('К1', False)]


def test_tick_cool_season_mirrored(env):
    _season('cool')
    _thermo()
    env['temp'] = 25.0                                   # target 24: too warm -> cool
    app_module._thermostat_tick(NOW)
    assert env['sets'] == [('К1', True)]
    ConvectorLog.query.delete()
    db.session.commit()
    env['sets'].clear()
    env['temp'] = 23.0
    env['status']['К1'] = {'online': True, 'is_on': True, 'power_w': None}
    app_module._thermostat_tick(NOW)
    assert env['sets'] == [('К1', False)]


def test_tick_min_cycle_blocks(env):
    _season('heat')
    _thermo()
    c = Convector.query.one()
    db.session.add(ConvectorLog(convector_id=c.id, ts=int(time.time()) - 60, is_on=False))
    db.session.commit()
    env['temp'] = 19.0
    app_module._thermostat_tick(NOW)
    assert env['sets'] == []
    ConvectorLog.query.update({'ts': int(time.time()) - 6 * 60})
    db.session.commit()
    app_module._thermostat_tick(NOW)
    assert env['sets'] == [('К1', True)]


def test_tick_sensor_missing_turns_off(env):
    _season('heat')
    _thermo(sensor=False)
    c = Convector.query.one()
    db.session.add(ConvectorLog(convector_id=c.id, ts=int(time.time()) - 60, is_on=True))   # inside the minimum time
    db.session.commit()
    env['status']['К1'] = {'online': True, 'is_on': True, 'power_w': None}
    app_module._thermostat_tick(NOW)
    assert env['sets'] == [('К1', False)]

    env['sets'].clear()
    _thermo(convs=('К2',))
    env['status']['К2'] = {'online': True, 'is_on': True, 'power_w': None}
    env['temp'], env['age'] = 15.0, 41 * 60            # cold, but the reading is 41 min old
    app_module._thermostat_tick(NOW)
    assert ('К2', False) in env['sets']


def test_tick_circuit_filter(env):
    _season('cool')
    _thermo(convs=(('Отопл', 'heating'), ('Охл', 'cooling'), ('Двата', 'both')))
    env['temp'] = 30.0
    app_module._thermostat_tick(NOW)
    assert sorted(env['sets']) == [('Двата', True), ('Охл', True)]


def test_tick_device_error_is_isolated(env):
    _season('heat')
    _thermo(convs=('Лош', 'Добър'))
    env['fail'].add('Лош')
    env['temp'] = 18.0
    app_module._thermostat_tick(NOW)
    assert env['sets'] == [('Добър', True)]
    assert _logs('Лош') == [] and len(_logs('Добър')) == 1


def test_tick_disabled_or_season_off_no_commands(env):
    _thermo()                                            # season unset = off
    env['temp'] = 10.0
    app_module._thermostat_tick(NOW)
    _season('off')
    app_module._thermostat_tick(NOW)
    _season('heat')
    Thermostat.query.update({'enabled': False})
    db.session.commit()
    app_module._thermostat_tick(NOW)
    assert env['sets'] == []


def test_tick_writes_log_with_source(env):
    _season('heat')
    _thermo()
    env['temp'] = 18.0
    before = int(time.time())
    app_module._thermostat_tick(NOW)
    (row,) = _logs('К1')
    assert row.source == 'thermostat' and row.is_on is True and row.ts >= before


def test_tick_unknown_state_uses_log_and_resends(env):
    _season('heat')
    _thermo()
    c = Convector.query.one()
    c.connection_type = 'mqtt'
    db.session.add(ConvectorLog(convector_id=c.id, ts=int(time.time()) - 300, is_on=True, source='thermostat'))
    db.session.commit()
    env['status']['К1'] = {'online': False, 'is_on': None, 'power_w': None}
    env['temp'] = 18.0                                   # wants on, log says on 5 min ago -> nothing
    app_module._thermostat_tick(NOW)
    assert env['sets'] == []
    ConvectorLog.query.update({'ts': int(time.time()) - 700})
    db.session.commit()
    app_module._thermostat_tick(NOW)                     # no confirmation for > 10 min -> send again
    assert env['sets'] == [('К1', True)]
    c = Convector.query.one()
    c.connection_type = 'ip'                             # an unreachable HTTP relay is skipped
    ConvectorLog.query.update({'ts': int(time.time()) - 700})
    db.session.commit()
    env['sets'].clear()
    app_module._thermostat_tick(NOW)
    assert env['sets'] == []


def test_override_until_is_utc_compared_with_local_now(env):
    _season('heat')
    t = _thermo()
    now_local = datetime.datetime.now(APP_TZ).replace(tzinfo=None)
    t.override_temp, t.override_until = 25.0, datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    db.session.commit()
    st = app_module._thermostat_state(t, now_local)
    assert (st['target'], st['source']) == (25.0, 'override')
    t.override_until = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
    db.session.commit()
    st = app_module._thermostat_state(t, now_local)
    assert (st['target'], st['source']) == (21.0, 'schedule')
    t.override_temp, t.hold = 40.0, True                 # clamped to the heating range
    db.session.commit()
    assert app_module._thermostat_state(t, now_local)['target'] == 30.0


def test_sensor_reading_falls_back_to_stored_reading(env, monkeypatch):
    from app import TemperatureReading
    s = TemperatureSensor.query.filter_by(mqtt_topic='t/hvac-tick').first() or TemperatureSensor(name='S', mqtt_topic='t/hvac-tick')
    db.session.add(s)
    db.session.commit()
    assert app_module._sensor_reading(None) == {'temp': None, 'humidity': None, 'age_s': None}
    r = app_module._sensor_reading(s)                    # live MQTT cache (UTC datetime last_seen, 60 s old)
    assert r['temp'] == 20.0 and r['humidity'] == 40.0 and 55 <= r['age_s'] <= 120
    assert isinstance(app_module._mqtt_temp_snapshot(s)['last_seen'], str)   # why the cache is read raw
    env['age'] = None                                    # restart: MQTT cache empty, value from the DB
    TemperatureReading.query.filter_by(sensor_id=s.id).delete()
    db.session.add(TemperatureReading(sensor_id=s.id, ts=int(time.time()) - 600, temperature=20.0))
    db.session.commit()
    r = app_module._sensor_reading(s)
    assert r['temp'] == 20.0 and 590 <= r['age_s'] <= 700


def _admin_client():
    from app import User, limiter
    from werkzeug.security import generate_password_hash
    limiter.reset()
    for name, role in (('hvac_admin', 'admin'), ('hvac_worker', 'worker')):
        if not User.query.filter_by(username=name).first():
            db.session.add(User(username=name, password=generate_password_hash('x123456'), role=role))
    db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'hvac_admin', 'password': 'x123456'})
    return c


def test_season_writes_betriebsart_and_clears_overrides(env, monkeypatch):
    writes = []
    monkeypatch.setattr(app_module, '_heatpump_write', lambda n, v: writes.append((n, v)) or {'ok': True, 'value': v})
    t = _thermo(override_temp=25.0, hold=True)
    tid = t.id
    c = _admin_client()
    r = c.post('/admin/hvac/season', data={'season': 'cool'})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    assert writes == [('Betriebsart', 2)]
    db.session.expire_all()
    assert app_module.hvac_season() == 'cool'
    t = db.session.get(Thermostat, tid)
    assert t.override_temp is None and t.override_until is None and t.hold is False
    assert env['sets'] == []
    assert c.post('/admin/hvac/season', data={'season': 'лято'}).status_code == 400
    from flask import g
    g.pop('_login_user', None)                           # the fixture's app context is shared with requests: drop the admin
    w = app.test_client()
    w.post('/login', data={'username': 'hvac_worker', 'password': 'x123456'})
    assert w.post('/admin/hvac/season', data={'season': 'heat'}).status_code == 302
    assert writes == [('Betriebsart', 2)]


def test_season_off_switches_convectors_off(env, monkeypatch):
    monkeypatch.setattr(app_module, '_heatpump_write', lambda n, v: {'ok': True, 'value': v})
    _season('heat')
    _thermo(convs=('К1', 'К2'))
    db.session.add(Convector(name='Свободен'))          # not on a thermostat: untouched
    _thermo(convs=('Ръчен',), enabled=False)             # disabled thermostat: its convector is left alone
    db.session.commit()
    res = app_module._hvac_set_season('off')
    assert res['ok'] is True and app_module.hvac_season() == 'off'
    assert sorted(env['sets']) == [('К1', False), ('К2', False)]
    assert [r.source for r in _logs('К1')] == ['thermostat']


def test_season_refused_when_pump_offline(env, monkeypatch):
    monkeypatch.setattr(app_module, '_heatpump_write',
                        lambda n, v: {'ok': False, 'error': 'Няма връзка с термопомпата.', 'status': 503})
    _season('heat')
    t = _thermo(override_temp=25.0, hold=True)
    tid = t.id
    c = _admin_client()
    r = c.post('/admin/hvac/season', data={'season': 'cool'})
    assert r.status_code == 503 and r.get_json()['ok'] is False
    db.session.expire_all()
    assert app_module.hvac_season() == 'heat'
    assert db.session.get(Thermostat, tid).hold is True


def test_manual_toggle_logs_source_manual(env):
    c = _admin_client()
    conv = Convector(name='Ръчен')
    db.session.add(conv)
    db.session.commit()
    r = c.post(f'/admin/convectors/{conv.id}/toggle')
    assert r.status_code in (302, 303) and env['sets'] == [('Ръчен', True)]
    assert [(x.is_on, x.source) for x in _logs('Ръчен')] == [(True, 'manual')]


# ---------------- Task 4: menu + Термостати admin page ----------------
@pytest.fixture
def adm(env):
    from app import User, limiter
    from werkzeug.security import generate_password_hash
    limiter.reset()
    if not User.query.filter_by(username='hvac_admin').first():
        db.session.add(User(username='hvac_admin', password=generate_password_hash('x123456'), role='admin'))
        db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'hvac_admin', 'password': 'x123456'})
    return c


def _flashes(c):
    with c.session_transaction() as s:
        return [m for _, m in s.get('_flashes', [])]


def test_thermostat_crud_and_assignment(adm):
    c1, c2 = Convector(name='А1'), Convector(name='А2')
    db.session.add_all([c1, c2])
    db.session.commit()
    r = adm.post('/admin/thermostats/save', data={'name': 'Цех', 'hysteresis': '0.8', 'min_cycle_min': '7',
                                                  'lowered_heat': '15', 'lowered_cool': '', 'enabled': '1',
                                                  'convector_ids': [str(c1.id), str(c2.id)]})
    assert r.status_code in (302, 303)
    t = Thermostat.query.filter_by(name='Цех').one()
    assert (t.hysteresis, t.min_cycle_min, t.lowered_heat, t.lowered_cool, t.enabled) == (0.8, 7, 15.0, None, True)
    assert {x.name for x in t.convectors} == {'А1', 'А2'}
    assert 'Цех' in adm.get('/admin/thermostats').get_data(as_text=True)
    # edit: unchecked convector goes free; bad hysteresis refused
    adm.post('/admin/thermostats/save', data={'id': t.id, 'name': 'Цех2', 'hysteresis': '0.5', 'min_cycle_min': '5',
                                              'lowered_heat': '16', 'convector_ids': [str(c1.id)]})
    db.session.expire_all()
    assert Thermostat.query.get(t.id).name == 'Цех2' and Convector.query.get(c2.id).thermostat_id is None
    adm.post('/admin/thermostats/save', data={'id': t.id, 'name': 'X', 'hysteresis': '9', 'min_cycle_min': '5', 'lowered_heat': '16'})
    db.session.expire_all()
    assert Thermostat.query.get(t.id).name == 'Цех2'
    db.session.add(ThermostatInterval(thermostat_id=t.id, season='heat', weekday=0, start_min=0, end_min=60, temp=20))
    db.session.commit()
    adm.post(f'/admin/thermostats/{t.id}/delete')
    db.session.expire_all()
    assert Thermostat.query.count() == 0 and ThermostatInterval.query.count() == 0
    assert Convector.query.get(c1.id).thermostat_id is None


def _t():
    t = Thermostat(name='Т')
    db.session.add(t)
    db.session.commit()
    return t


def _rows(*r):
    import json
    return json.dumps([{'weekday': w, 'start': s, 'end': e, 'temp': tp} for w, s, e, tp in r])


def test_schedule_save_validates(adm):
    t = _t()
    url = f'/admin/thermostats/{t.id}/schedule'
    adm.post(url, data={'season': 'heat', 'rows_json': _rows((0, '07:00', '17:00', 21), (1, '07:00', '24:00', 40))})
    assert ThermostatInterval.query.count() == 2
    # clamped: 40 -> 30
    assert sorted(i.temp for i in ThermostatInterval.query) == [21.0, 30.0]
    # overlap -> nothing replaced
    adm.post(url, data={'season': 'heat', 'rows_json': _rows((0, '07:00', '12:00', 21), (0, '11:00', '13:00', 21))})
    assert ThermostatInterval.query.count() == 2 and _flashes(adm)
    # garbage input
    for bad in (_rows((9, '07:00', '08:00', 21)), _rows((0, '7h', '08:00', 21)), _rows((0, '07:00', '08:00', 'x')), 'nope'):
        adm.post(url, data={'season': 'heat', 'rows_json': bad})
        assert ThermostatInterval.query.count() == 2
    # another season is untouched by a replace
    adm.post(url, data={'season': 'cool', 'rows_json': _rows((2, '08:00', '16:00', 24))})
    adm.post(url, data={'season': 'heat', 'rows_json': '[]'})
    assert [(i.season, i.weekday) for i in ThermostatInterval.query] == [('cool', 2)]


def test_schedule_copy(adm):
    a, b = _t(), _t()
    adm.post(f'/admin/thermostats/{a.id}/schedule', data={'season': 'heat', 'rows_json': _rows((0, '07:00', '17:00', 21))})
    db.session.add(ThermostatInterval(thermostat_id=b.id, season='heat', weekday=3, start_min=0, end_min=60, temp=18))
    db.session.commit()
    adm.post(f'/admin/thermostats/{a.id}/schedule/copy', data={'season': 'heat', 'target_ids': [str(b.id)]})
    assert [(i.weekday, i.start_min, i.end_min, i.temp) for i in ThermostatInterval.query.filter_by(thermostat_id=b.id)] == [(0, 420, 1020, 21.0)]


def test_holiday_add_delete(adm):
    adm.post('/admin/hvac/holidays', data={'day': '2026-12-25', 'name': 'Коледа'})
    h = HeatingHoliday.query.one()
    assert h.day == datetime.date(2026, 12, 25)
    adm.post('/admin/hvac/holidays', data={'day': '2026-12-25', 'name': 'пак'})   # duplicate: no crash
    assert HeatingHoliday.query.count() == 1
    assert 'Коледа' in adm.get('/admin/thermostats').get_data(as_text=True)
    adm.post(f'/admin/hvac/holidays/{h.id}/delete')
    assert HeatingHoliday.query.count() == 0


def test_room_delete_nulls_thermostat_room(adm):
    from app import Building, Room
    b = Building(name='Б')
    db.session.add(b)
    db.session.flush()
    r = Room(name='Р', building_id=b.id)
    db.session.add(r)
    db.session.flush()
    t = Thermostat(name='ТР', room_id=r.id)
    db.session.add(t)
    db.session.commit()
    adm.post(f'/admin/rooms/{r.id}/delete')
    db.session.expire_all()
    assert Thermostat.query.get(t.id).room_id is None


def test_navbar_has_hvac_section(adm):
    html = adm.get('/admin/thermostats').get_data(as_text=True)
    i = html.index('dropdown-sub-trigger">Отопление и охлаждане')
    block = html[i:html.index('</div>', i)]
    for label in ('Табло', 'Термостати', 'Температурни сензори', 'Конвектори', 'Термопомпа'):
        assert label in block
    assert block.index('href="/admin/hvac"') < block.index('href="/admin/thermostats"')   # the dashboard comes first
    # old standalone links are no longer direct children of "Съоръжения и енергия"
    j = html.index('Съоръжения и енергия')
    outer = html[j:i]
    assert 'href="/admin/convectors"' not in outer and 'href="/admin/temperature-sensors"' not in outer


def test_remove_room_helper_nulls_thermostat_room(adm):
    from app import Building, Room, _remove_room
    b = Building(name='Б2')
    db.session.add(b)
    db.session.flush()
    r = Room(name='Р2', building_id=b.id)
    db.session.add(r)
    db.session.flush()
    t = Thermostat(name='ТР2', room_id=r.id)
    db.session.add(t)
    db.session.commit()
    _remove_room(r)
    db.session.commit()
    db.session.expire_all()
    assert db.session.get(Thermostat, t.id).room_id is None


def test_thermostat_save_rejects_nan(adm):
    for k in ('hysteresis', 'lowered_heat', 'lowered_cool'):
        d = {'name': 'NaN', 'hysteresis': '0.5', 'min_cycle_min': '5', 'lowered_heat': '16', k: 'nan'}
        adm.post('/admin/thermostats/save', data=d)
    assert Thermostat.query.filter_by(name='NaN').count() == 0


# ---------------- Task 5: dashboard /admin/hvac ----------------
def _login(role):
    """Fresh client logged in as hvac_admin / hvac_worker (drops the user cached on the shared app context)."""
    from flask import g
    _admin_client()
    g.pop('_login_user', None)
    c = app.test_client()
    c.post('/login', data={'username': f'hvac_{role}', 'password': 'x123456'})
    return c


def test_dashboard_loads_for_worker_and_admin(env):
    _season('heat')
    _thermo()
    a = _login('admin').get('/admin/hvac')
    assert a.status_code == 200
    html = a.get_data(as_text=True)
    assert 'data-season-btn="cool"' in html and 'startPoll' in html
    w = _login('worker').get('/admin/hvac')
    assert w.status_code == 200
    assert '<button type="button" data-season-btn=' not in w.get_data(as_text=True)    # no season buttons


def test_data_hides_pump_from_worker(env, monkeypatch):
    _season('heat')
    _thermo(convs=('К1', 'К2'))
    env['status']['К1'] = {'online': True, 'is_on': True, 'power_w': 850.0}
    monkeypatch.setitem(app_module._heatpump_live, 'online', True)
    monkeypatch.setitem(app_module._heatpump_live, 'slow', {'Betriebsart': 1})
    monkeypatch.setitem(app_module._heatpump_live, 'data', {'Temp. Vorlauf': 35.0, 'Temp. Aussen': 4.0})
    monkeypatch.setattr(app_module, '_heatpump_power', lambda: {'w': 2100, 'online': True})
    d = _login('admin').get('/admin/hvac/data').get_json()
    assert d['season'] == 'heat' and d['season_label'] == 'Отопление'
    assert d['pump']['supply'] == 35.0 and d['pump']['outdoor'] == 4.0 and d['pump']['power_w'] == 2100
    assert d['pump']['mismatch'] is False and d['pump']['mode_label'] == 'Автоматика'
    t = d['thermostats'][0]
    assert t['name'] == 'Цех' and t['target'] == 21.0 and t['group'] == 'Без помещение' and t['enabled'] is True
    assert {c['name']: (c['is_on'], c['power_w']) for c in t['convectors']} == {'К1': (True, 850.0), 'К2': (False, None)}
    assert d['convectors_on'] == 1
    w = _login('worker').get('/admin/hvac/data').get_json()
    assert w['pump'] is None and w['thermostats'][0]['name'] == 'Цех'


def test_data_mismatch_flag(env, monkeypatch):
    _season('heat')
    monkeypatch.setitem(app_module._heatpump_live, 'online', True)
    monkeypatch.setitem(app_module._heatpump_live, 'slow', {'Betriebsart': 2})
    monkeypatch.setattr(app_module, '_heatpump_power', lambda: None)
    c = _login('admin')
    assert c.get('/admin/hvac/data').get_json()['pump']['mismatch'] is True
    monkeypatch.setitem(app_module._heatpump_live, 'online', False)
    assert c.get('/admin/hvac/data').get_json()['pump']['mismatch'] is False


def test_override_plus_minus_clamped_and_capped(env):
    _season('heat')
    t = _thermo(heat=29.8)                               # whole-week 29.8 schedule: no next change -> 24 h cap
    tid = t.id
    c = _login('worker')
    url = f'/admin/hvac/{tid}/override'
    st = c.post(url, data={'action': 'plus'}).get_json()
    assert st['target'] == 30.0 and st['source'] == 'override'
    assert c.post(url, data={'action': 'plus'}).get_json()['target'] == 30.0    # clamped
    assert c.post(url, data={'action': 'minus'}).get_json()['target'] == 29.5
    db.session.expire_all()
    t = db.session.get(Thermostat, tid)
    left = t.override_until - datetime.datetime.utcnow()
    assert datetime.timedelta(hours=23, minutes=58) < left <= datetime.timedelta(hours=24)
    assert c.post(url, data={'action': 'set', 'temp': '2'}).get_json()['target'] == 5.0
    assert c.post(url, data={'action': 'set', 'temp': 'nan'}).status_code == 400
    assert c.post(url, data={'action': 'hold'}).get_json()['source'] == 'hold'
    db.session.expire_all()
    assert db.session.get(Thermostat, tid).hold is True
    st = c.post(url, data={'action': 'release'}).get_json()
    assert (st['target'], st['source']) == (29.8, 'schedule')
    db.session.expire_all()
    t = db.session.get(Thermostat, tid)
    assert t.override_temp is None and t.override_until is None and t.hold is False
    _season('off')
    assert c.post(url, data={'action': 'plus'}).status_code == 400


def test_override_until_next_schedule_change(env):
    _season('heat')
    t = Thermostat(name='Г')
    now = datetime.datetime.now(APP_TZ).replace(tzinfo=None)
    end = now.hour * 60 + now.minute + 90
    if end >= 1440:
        return                                           # too close to midnight for this check
    t.intervals.append(ThermostatInterval(season='heat', weekday=now.weekday(), start_min=0, end_min=end, temp=21))
    db.session.add(t)
    db.session.commit()
    _login('worker').post(f'/admin/hvac/{t.id}/override', data={'action': 'plus'})
    db.session.expire_all()
    until_local = app_module.to_local(db.session.get(Thermostat, t.id).override_until).replace(tzinfo=None)
    expected = datetime.datetime.combine(now.date(), datetime.time()) + datetime.timedelta(minutes=end)
    assert abs((until_local - expected).total_seconds()) < 2


def test_enabled_toggle_admin_only(env):
    t = _thermo()
    tid = t.id
    assert _login('worker').post(f'/admin/thermostats/{tid}/enabled', data={'enabled': '0'}).status_code == 302
    db.session.expire_all()
    assert db.session.get(Thermostat, tid).enabled is True
    r = _login('admin').post(f'/admin/thermostats/{tid}/enabled', data={'enabled': '0'})
    assert r.status_code == 200 and r.get_json()['enabled'] is False
    db.session.expire_all()
    assert db.session.get(Thermostat, tid).enabled is False


def test_worker_cannot_change_season_or_schedule(env, monkeypatch):
    writes = []
    monkeypatch.setattr(app_module, '_heatpump_write', lambda n, v: writes.append((n, v)) or {'ok': True, 'value': v})
    _season('heat')
    t = _thermo()
    tid = t.id
    w = _login('worker')
    assert w.post('/admin/hvac/season', data={'season': 'cool'}).status_code == 302
    assert w.post('/admin/thermostats/save', data={'id': tid, 'name': 'Хак', 'hysteresis': '1', 'min_cycle_min': '5',
                                                   'lowered_heat': '16'}).status_code == 302
    assert w.post(f'/admin/thermostats/{tid}/schedule',
                  data={'season': 'heat', 'rows_json': _rows((0, '07:00', '08:00', 25))}).status_code == 302
    db.session.expire_all()
    assert writes == [] and app_module.hvac_season() == 'heat'
    t = db.session.get(Thermostat, tid)
    assert t.name == 'Цех' and len([i for i in t.intervals if i.season == 'heat']) == 7


def test_history_shape(env):
    from app import TemperatureReading
    _season('heat')
    t = _thermo(convs=('К1', 'К2'))
    now = int(time.time())
    k1, k2 = (Convector.query.filter_by(name=n).one().id for n in ('К1', 'К2'))
    db.session.add_all([TemperatureReading(sensor_id=t.sensor_id, ts=now - 7200, temperature=19.5),
                        TemperatureReading(sensor_id=t.sensor_id, ts=now - 3600, temperature=20.5),
                        TemperatureReading(sensor_id=t.sensor_id, ts=now - 100 * 3600, temperature=10.0),
                        ConvectorLog(convector_id=k1, ts=now - 30 * 3600, is_on=True),      # on since before the window
                        ConvectorLog(convector_id=k1, ts=now - 5000, is_on=False),
                        ConvectorLog(convector_id=k2, ts=now - 6000, is_on=True),
                        ConvectorLog(convector_id=k2, ts=now - 2000, is_on=False)])
    db.session.commit()
    d = _login('worker').get(f'/admin/hvac/{t.id}/history?hours=24').get_json()
    assert d['temps'] == [[now - 7200, 19.5], [now - 3600, 20.5]]
    assert d['target'] and all(v == 21.0 for _, v in d['target'])
    assert len(d['on']) == 1
    a, b = d['on'][0]
    assert abs(a - (now - 24 * 3600)) < 5 and b == now - 2000          # union of the two overlapping periods
