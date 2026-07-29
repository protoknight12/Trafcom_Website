"""
One-off schema migration: adds a VAT/ДДС number field to Supplier
(already has eik, was missing vat_number).

    python -m migration.migrate_add_supplier_vat

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE supplier ADD COLUMN IF NOT EXISTS vat_number VARCHAR(20)
    '''))
    db.session.commit()

print("supplier.vat_number added (or already existed).")
