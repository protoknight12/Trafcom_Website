"""
One-off schema migration: adds legal-entity (юридическо лице) fields to
Deliverer (куриер), same fields as Client.

    python migrate_add_deliverer_legal_fields.py

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE deliverer
        ADD COLUMN IF NOT EXISTS eik VARCHAR(20),
        ADD COLUMN IF NOT EXISTS vat_number VARCHAR(20),
        ADD COLUMN IF NOT EXISTS address VARCHAR(255),
        ADD COLUMN IF NOT EXISTS mol VARCHAR(150)
    '''))
    db.session.commit()

print("deliverer.eik/vat_number/address/mol added (or already existed).")
