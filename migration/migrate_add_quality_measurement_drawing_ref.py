"""
One-off schema migration: adds a `drawing_ref` column to
QualityMeasurement - free-text reference to the dimension's balloon/callout
number on the technical drawing (see admin_quality_control.html's "№ от
чертежа" field per measurement row).

    python -m migration.migrate_add_quality_measurement_drawing_ref

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE quality_measurement ADD COLUMN IF NOT EXISTS drawing_ref VARCHAR(50)
    '''))
    db.session.commit()

print("quality_measurement.drawing_ref added (or already existed).")
