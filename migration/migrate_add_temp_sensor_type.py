"""
One-off schema migration: adds `sensor_type` to TemperatureSensor - existing
rows all default to 'shelly_ht_gen1' (the only format the app understood
before this), but since the user's actual sensors are a mix of different
hardware, each should be reviewed and corrected via the edit popup on
/admin/temperature-sensors (see TEMP_SENSOR_TYPES in app.py for the options).

    python -m migration.migrate_add_temp_sensor_type

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE temperature_sensor ADD COLUMN IF NOT EXISTS sensor_type VARCHAR(20) NOT NULL DEFAULT 'shelly_ht_gen1'
    '''))
    db.session.commit()

print("temperature_sensor.sensor_type added (or already existed) - review each sensor's type on /admin/temperature-sensors.")
