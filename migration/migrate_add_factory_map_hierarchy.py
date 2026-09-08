"""
One-off schema migration: adds `room_id`/`panel_id` to Machine and
`panel_id` to ShellyDevice, for the Building -> Room -> Machine / Electrical
panel hierarchy on the interactive factory map (see Building, Room,
ElectricalPanel - brand-new tables db.create_all() creates on its own).
This only touches the pre-existing machine/shelly_device tables.

    python -m migration.migrate_add_factory_map_hierarchy

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE machine ADD COLUMN IF NOT EXISTS room_id INTEGER REFERENCES room(id)
    '''))
    db.session.execute(text('''
        ALTER TABLE machine ADD COLUMN IF NOT EXISTS panel_id INTEGER REFERENCES electrical_panel(id)
    '''))
    db.session.execute(text('''
        ALTER TABLE shelly_device ADD COLUMN IF NOT EXISTS panel_id INTEGER REFERENCES electrical_panel(id)
    '''))
    db.session.commit()

print("machine.room_id / machine.panel_id / shelly_device.panel_id added (or already existed).")
