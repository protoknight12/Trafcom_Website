"""
One-off schema migration: adds erp_number/code_number to the product and
material_price tables (for label printing, see print_label() in app.py).
Safe to run more than once (IF NOT EXISTS). Run this once with your normal
project environment active:

    python migrate_add_product_material_label_fields.py

(If you haven't already, also run migrate_add_detail_label_fields.py - the
detail table needs the same two columns.)
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE product
        ADD COLUMN IF NOT EXISTS erp_number VARCHAR(50),
        ADD COLUMN IF NOT EXISTS code_number VARCHAR(100)
    '''))
    db.session.execute(text('''
        ALTER TABLE material_price
        ADD COLUMN IF NOT EXISTS erp_number VARCHAR(50),
        ADD COLUMN IF NOT EXISTS code_number VARCHAR(100)
    '''))
    db.session.commit()

print("product and material_price columns added (or already existed).")
