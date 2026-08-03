"""
One-off schema migration: replaces ShellyDevice.machine_id (a single nullable
FK, added by migrate_add_shelly_device_machine.py) with a many-to-many
shelly_device_machine table - one meter can genuinely feed more than one
machine (a shared sub-panel/feed) and one machine can have more than one
meter on it, which a single FK column can't represent.

Creates the new table, copies any existing non-null machine_id values across
as the equivalent single-row link (so nothing already set is lost), then
drops the now-redundant column. Safe to run more than once.

    python -m migration.migrate_shelly_device_many_to_many
"""
from sqlalchemy import text, inspect

from app import app, db

with app.app_context():
    db.create_all()  # creates shelly_device_machine if this is also a brand-new install

    # machine_id may already be gone (script re-run) or may never have existed
    # (a fresh install created straight from this version's models) - only
    # copy data across if there's actually a column to copy it from.
    columns = {c['name'] for c in inspect(db.engine).get_columns('shelly_device')}
    if 'machine_id' in columns:
        db.session.execute(text('''
            INSERT INTO shelly_device_machine (shelly_device_id, machine_id)
            SELECT id, machine_id FROM shelly_device WHERE machine_id IS NOT NULL
            ON CONFLICT DO NOTHING
        '''))
        db.session.execute(text('ALTER TABLE shelly_device DROP COLUMN machine_id'))
        db.session.commit()

print("shelly_device_machine populated from any existing machine_id values; machine_id column dropped.")
