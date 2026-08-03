"""
One-off schema migration: links the power dashboard's ShellyDevice rows to
the Machine catalog.

ShellyDevice itself was a brand-new table (added alongside the /admin/power
dashboard), so db.create_all() already created it - only the new machine_id
column on that existing table needs a manual ALTER. Safe to run more than
once.

SUPERSEDED by migration/migrate_shelly_device_many_to_many.py, which came
right after this one: a single machine_id FK turned out to be the wrong
shape (one meter can feed more than one machine, and one machine can have
more than one meter), so that migration moves this column's data into a
many-to-many table and drops machine_id. Keep running both, in this order,
on any database that predates the many-to-many change - this one still has
to run first so there's a column for the next one to read from.

    python -m migration.migrate_add_shelly_device_machine
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.create_all()  # creates shelly_device if this is also a brand-new install
    db.session.execute(text('''
        ALTER TABLE shelly_device
        ADD COLUMN IF NOT EXISTS machine_id INTEGER REFERENCES machine(id)
    '''))
    db.session.commit()

print("shelly_device.machine_id added (or already existed).")
