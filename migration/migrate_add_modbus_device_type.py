"""
One-off schema migration: adds `device_type` to ModbusDevice, so a row can
declare which register map/snapshot function decodes it (see
_dtsu666_snapshot()/_solis_snapshot() in app.py). Existing rows default to
'dtsu666' - the only model this table supported before Solis S6 inverters
were added.

    python -m migration.migrate_add_modbus_device_type

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE modbus_device ADD COLUMN IF NOT EXISTS device_type VARCHAR(20) NOT NULL DEFAULT 'dtsu666'
    '''))
    db.session.commit()

print("modbus_device.device_type added (or already existed).")
