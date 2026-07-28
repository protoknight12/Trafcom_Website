"""
One-off schema migration: adds brand/type columns to material_price and
backfills every existing row to type='sheets' (all pre-existing materials
were sheet stock - see MATERIAL_TYPE_LABELS in app.py for the other
categories rods/profiles/pipes/other). Safe to run more than once.

    python migrate_add_material_brand_type.py
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE material_price
        ADD COLUMN IF NOT EXISTS brand VARCHAR(100),
        ADD COLUMN IF NOT EXISTS type VARCHAR(30)
    '''))
    db.session.execute(text('''
        UPDATE material_price SET type = 'sheets' WHERE type IS NULL
    '''))
    db.session.commit()

print("material_price.brand/type added and existing rows backfilled to 'sheets' (or already done).")
