"""
One-off schema migration for the hall plan hierarchy (parcel -> building -> room -> objects):
hall_shape.parcel_id / parent_id / building_id (link to the factory map's Building), hall_machine.parent_id, hall_equipment.parent_id (all NULL = derived from position;
the new hall_parcel table is created by db.create_all()). Safe to run more than once.

    python -m migration.migrate_add_hall_hierarchy
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.create_all()
    for stmt in (
        "ALTER TABLE hall_shape ADD COLUMN IF NOT EXISTS parcel_id INTEGER",
        "ALTER TABLE hall_shape ADD COLUMN IF NOT EXISTS parent_id INTEGER",
        "ALTER TABLE hall_shape ADD COLUMN IF NOT EXISTS building_id INTEGER",
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS parent_id INTEGER",
        "ALTER TABLE hall_equipment ADD COLUMN IF NOT EXISTS parent_id INTEGER",
    ):
        db.session.execute(text(stmt))
    db.session.commit()

print("hall hierarchy columns added (or already existed).")
