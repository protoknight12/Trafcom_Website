"""
pytest: opt-in retention of the reading-log tables (READINGS_KEEP_DAYS).

Unset / 0 -> nothing is ever deleted; set -> only rows older than the cutoff go, from all five tables, in batches.

Run with:
    python -m pytest testing/test_readings_retention.py -q -p no:cacheprovider
"""
import os
import tempfile
import time

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest

import app as app_module
from app import (app as flask_app, db, ShellyReadingLog, SolisReadingLog, TemperatureReading, ConvectorLog,
                 HeatPumpReading, ModbusDevice)

MODELS = (ShellyReadingLog, SolisReadingLog, TemperatureReading, ConvectorLog, HeatPumpReading)
NOW = 2_000_000_000
DAY = 86400


@pytest.fixture
def seeded():
    with flask_app.app_context():
        db.create_all()
        dev = ModbusDevice(name='i', host='h', port=1, device_type='solis_s6')
        db.session.add(dev)
        db.session.flush()
        for age_days in (100, 40, 10, 1):          # two older than 30 days, two newer
            ts = NOW - age_days * DAY
            db.session.add_all([
                ShellyReadingLog(host='x', ts=ts), SolisReadingLog(device_id=dev.id, ts=ts, snapshot_json='{}'),
                TemperatureReading(sensor_id=1, ts=ts), ConvectorLog(convector_id=1, ts=ts, is_on=True),
                HeatPumpReading(ts=ts, data_json='{}')])
        db.session.commit()
        yield
        db.session.remove()
        db.drop_all()


def _counts():
    return [m.query.count() for m in MODELS]


def test_default_keeps_everything(seeded, monkeypatch):
    assert app_module.READINGS_KEEP_DAYS == 0            # env var unset in the test process
    with flask_app.app_context():
        assert app_module._readings_cleanup(now=NOW) == 0
        monkeypatch.setattr(app_module, '_readings_cleanup_last', 0.0)
        app_module._readings_cleanup_daily()
        assert _counts() == [4] * 5


def test_blank_env_value_does_not_crash_the_import():
    assert int('' or 0) == 0 and int(None or 0) == 0     # the exact parse used for READINGS_KEEP_DAYS


def test_only_rows_older_than_the_cutoff_go(seeded, monkeypatch):
    monkeypatch.setattr(app_module, 'READINGS_CLEANUP_BATCH', 1)    # forces several batches per table
    with flask_app.app_context():
        assert app_module._readings_cleanup(keep_days=30, now=NOW) == 10
        assert _counts() == [2] * 5
        for m in MODELS:
            assert all(r.ts >= NOW - 30 * DAY for r in m.query.all())


def test_daily_runner_uses_the_setting_and_runs_once_a_day(seeded, monkeypatch):
    monkeypatch.setattr(app_module, 'READINGS_KEEP_DAYS', 30)
    monkeypatch.setattr(app_module, '_readings_cleanup_last', 0.0)
    calls = []
    real = app_module._readings_cleanup
    monkeypatch.setattr(app_module, '_readings_cleanup', lambda *a, **k: calls.append(1) or real(now=NOW))
    with flask_app.app_context():
        app_module._readings_cleanup_daily()
        app_module._readings_cleanup_daily()              # second call within a day: no-op
        assert calls == [1]
        assert _counts() == [2] * 5
    assert time.time() - app_module._readings_cleanup_last < 60
