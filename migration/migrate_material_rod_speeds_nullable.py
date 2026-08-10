"""
One-off schema migration: drops the NOT NULL constraint on
material_price.cutting_speed_mm_per_min and pierce_rate_per_min, and clears
both for existing 'rods' rows - rod stock is cut to length on a saw, never
pierced or DXF-cut, so it carries neither a cutting speed nor a pierce/drill
speed (see app.py's MaterialPrice model and _parse_material_type callers).
Safe to run more than once.

    python -m migration.migrate_material_rod_speeds_nullable
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE material_price ALTER COLUMN cutting_speed_mm_per_min DROP NOT NULL
    '''))
    db.session.execute(text('''
        ALTER TABLE material_price ALTER COLUMN pierce_rate_per_min DROP NOT NULL
    '''))
    db.session.execute(text('''
        UPDATE material_price SET cutting_speed_mm_per_min = NULL, pierce_rate_per_min = NULL WHERE type = 'rods'
    '''))
    db.session.commit()

print("material_price.cutting_speed_mm_per_min/pierce_rate_per_min are now nullable and cleared for existing 'rods' rows (or already done).")
