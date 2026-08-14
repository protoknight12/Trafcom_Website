"""
One-off schema migration: adds material_price.height_mm - the 4th profile
dimension (height, alongside sheet_width_mm=width/sheet_length_mm=length/
thickness_mm=wall thickness) needed because a profile's cross-section isn't
fully described by the 3 generic dimension columns the way sheets/rods/pipes
are. Safe to run more than once.

    python -m migration.migrate_add_material_height
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE material_price
        ADD COLUMN IF NOT EXISTS height_mm FLOAT
    '''))
    db.session.commit()

print("material_price.height_mm added (or already existed).")
