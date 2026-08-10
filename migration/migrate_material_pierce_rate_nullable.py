"""
One-off schema migration: drops the NOT NULL constraint on
material_price.pierce_rate_per_min and clears it for existing 'rods' rows -
rod stock is cut to length, never pierced, so it carries no pierce/drill
speed at all (see app.py's MaterialPrice model and _parse_material_type
callers). Safe to run more than once.

    python -m migration.migrate_material_pierce_rate_nullable
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE material_price ALTER COLUMN pierce_rate_per_min DROP NOT NULL
    '''))
    db.session.execute(text('''
        UPDATE material_price SET pierce_rate_per_min = NULL WHERE type = 'rods'
    '''))
    db.session.commit()

print("material_price.pierce_rate_per_min is now nullable and cleared for existing 'rods' rows (or already done).")
