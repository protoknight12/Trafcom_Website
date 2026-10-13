"""
One-off schema migration: drops the NOT NULL constraint on
material_price.cutting_speed_mm_per_min and pierce_rate_per_min - rod stock is
cut to length on a saw, never pierced or DXF-cut, so it carries neither a
cutting speed nor a pierce/drill speed (see app.py's MaterialPrice model and
_parse_material_type callers). Safe to run more than once.

It used to also NULL both columns for every 'rods' row; that UPDATE is gone
because material types are admin-editable now and it wiped speeds an admin had
entered (a new database has no legacy rows to clear).

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
    db.session.commit()

print("material_price.cutting_speed_mm_per_min/pierce_rate_per_min are now nullable (or already done).")
