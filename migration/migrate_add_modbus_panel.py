"""
One-off schema migration: adds `panel_id` to ModbusDevice, so a Modbus meter
can be assigned to an ElectricalPanel the same way ShellyDevice already can
(the modbus_device_machine link table is brand new - db.create_all() creates
it on its own). This only touches the pre-existing modbus_device table.

    python -m migration.migrate_add_modbus_panel

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE modbus_device ADD COLUMN IF NOT EXISTS panel_id INTEGER REFERENCES electrical_panel(id)
    '''))
    db.session.commit()

print("modbus_device.panel_id added (or already existed).")
