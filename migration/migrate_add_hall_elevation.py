"""
One-off schema migration for the hall 3D plan: hall_machine.elevation, hall_shape.elevation and
hall_shape.floors (bottom above the floor in metres / number of storeys of a room). Also gives the
rooms that were seeded with height 0 a real per-floor height. Safe to run more than once.
(The new hall_equipment table is created by db.create_all().)

    python -m migration.migrate_add_hall_elevation
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.create_all()
    for stmt in (
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS elevation FLOAT NOT NULL DEFAULT 0",
        "ALTER TABLE hall_shape ADD COLUMN IF NOT EXISTS elevation FLOAT NOT NULL DEFAULT 0",
        "ALTER TABLE hall_shape ADD COLUMN IF NOT EXISTS floors INTEGER NOT NULL DEFAULT 1",
        "UPDATE hall_shape SET height = 5 WHERE kind = 'room' AND height = 0",
    ):
        db.session.execute(text(stmt))
    db.session.commit()

print("hall_machine/hall_shape elevation + hall_shape.floors added (or already existed).")
