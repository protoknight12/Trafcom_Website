"""
One-off schema migration: adds pos_x/pos_y to Convector - its dragged
position on its room's map (admin_factory_map_room.html), same percent-of-
canvas convention as Machine.pos_x/pos_y.

    python -m migration.migrate_add_convector_position

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE convector ADD COLUMN IF NOT EXISTS pos_x DOUBLE PRECISION'))
    db.session.execute(text('ALTER TABLE convector ADD COLUMN IF NOT EXISTS pos_y DOUBLE PRECISION'))
    db.session.commit()

print("convector.pos_x/pos_y added (or already existed).")
