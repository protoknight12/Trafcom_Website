"""
One-off schema migration: adds the sheet_length_mm/sheet_width_mm/thickness_mm
columns to material_price. Safe to run more than once (IF NOT EXISTS).
Run this once with your normal project environment active:

    python migrate_add_sheet_dimensions.py
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE material_price
        ADD COLUMN IF NOT EXISTS sheet_length_mm FLOAT,
        ADD COLUMN IF NOT EXISTS sheet_width_mm FLOAT,
        ADD COLUMN IF NOT EXISTS thickness_mm FLOAT
    '''))
    db.session.commit()

print("material_price columns added (or already existed).")
