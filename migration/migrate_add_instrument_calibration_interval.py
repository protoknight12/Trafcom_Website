"""
One-off schema migration: adds `calibration_interval_months` to
MeasuringInstrument (ISO 9001 §7.1.5 calibration tracking - see
InstrumentCalibrationRecord, a brand-new table db.create_all() creates on
its own). This only touches the pre-existing measuring_instrument table.

    python -m migration.migrate_add_instrument_calibration_interval

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE measuring_instrument ADD COLUMN IF NOT EXISTS calibration_interval_months INTEGER
    '''))
    db.session.commit()

print("measuring_instrument.calibration_interval_months added (or already existed).")
