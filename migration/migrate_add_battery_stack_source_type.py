"""
One-off schema migration: adds battery_stack.source_type ('inverter' or
'modbus' - see BatteryStack's docstring in app.py), defaulting every
existing row to 'inverter' since that's the only interface this shop's
stacks have ever used (through inverter_device_id/bms_port).

    python -m migration.migrate_add_battery_stack_source_type

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE battery_stack ADD COLUMN IF NOT EXISTS source_type VARCHAR(20) NOT NULL DEFAULT 'inverter'
    '''))
    db.session.commit()

print("battery_stack.source_type added (or already existed).")
