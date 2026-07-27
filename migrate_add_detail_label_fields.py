"""
One-off schema migration: adds erp_number/code_number to the detail table
(for label printing, see print_label() in app.py). Safe to run more than
once (IF NOT EXISTS). Run this once with your normal project environment
active:

    python migrate_add_detail_label_fields.py
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE detail
        ADD COLUMN IF NOT EXISTS erp_number VARCHAR(50),
        ADD COLUMN IF NOT EXISTS code_number VARCHAR(100)
    '''))
    db.session.commit()

print("detail columns added (or already existed).")
