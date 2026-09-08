"""
One-off schema migration: adds battery_temperature/battery_fault_bits to
SolisReadingLog - two extra headline fields discovered via
github.com/Pho3niX90/solis_modbus's independently reverse-engineered Solis
register map (registers 33043 and 33118 - see SOLIS_BLOCK_METER_BATTERY in
app.py), added after the table already existed.

    python -m migration.migrate_add_solis_battery_extra_fields

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE solis_reading_log ADD COLUMN IF NOT EXISTS battery_temperature DOUBLE PRECISION
    '''))
    db.session.execute(text('''
        ALTER TABLE solis_reading_log ADD COLUMN IF NOT EXISTS battery_fault_bits INTEGER
    '''))
    db.session.commit()

print("solis_reading_log.battery_temperature/battery_fault_bits added (or already existed).")
