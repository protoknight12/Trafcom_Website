"""
One-off schema migration: adds an `instrument_id` FK column to
QualityMeasurement, linking each measurement to the MeasuringInstrument
(if any) it was taken with - see admin_quality_control.html's per-row
instrument picker. db.create_all() already created the new
measuring_instrument table itself (didn't exist before); this only
ALTERs the pre-existing quality_measurement table.

    python -m migration.migrate_add_quality_measurement_instrument

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE quality_measurement
        ADD COLUMN IF NOT EXISTS instrument_id INTEGER REFERENCES measuring_instrument(id)
    '''))
    db.session.commit()

print("quality_measurement.instrument_id added (or already existed).")
