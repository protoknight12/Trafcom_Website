"""
One-off schema migration: thermostats (heating/cooling). Creates the new tables (thermostat,
thermostat_interval, heating_holiday) through db.create_all(), then adds convector.thermostat_id
and convector_log.source. Safe to run more than once.

    python -m migration.migrate_add_thermostats
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.create_all()
    db.session.execute(text('ALTER TABLE convector ADD COLUMN IF NOT EXISTS thermostat_id INTEGER REFERENCES thermostat(id)'))
    db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_convector_thermostat_id ON convector (thermostat_id)'))
    db.session.execute(text('ALTER TABLE convector_log ADD COLUMN IF NOT EXISTS source VARCHAR(12)'))
    db.session.commit()

print('thermostat tables, convector.thermostat_id, convector_log.source added (or already existed).')
