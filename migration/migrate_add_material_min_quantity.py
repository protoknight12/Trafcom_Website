"""
One-off schema migration: adds the min_quantity column to material_price
(reorder threshold - see MaterialPrice.min_quantity). Safe to run more than
once (IF NOT EXISTS). Run this once with your normal project environment
active:

    python -m migration.migrate_add_material_min_quantity
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE material_price
        ADD COLUMN IF NOT EXISTS min_quantity FLOAT
    '''))
    db.session.commit()

print("material_price.min_quantity column added (or already existed).")
