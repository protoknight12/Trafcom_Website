"""
One-off schema migration: bend_step.length_mm (length of the step's bend line; NULL = the part's whole width). Only needed if the table already existed.

    python -m migration.migrate_add_bend_step_length

Safe to run more than once. Programs generated from a DXF afterwards get the length of every bend line; older steps keep NULL.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text("ALTER TABLE bend_step ADD COLUMN IF NOT EXISTS length_mm DOUBLE PRECISION"))
    db.session.commit()

print("bend_step.length_mm added (or already existed).")
