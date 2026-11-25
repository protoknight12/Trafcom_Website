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
