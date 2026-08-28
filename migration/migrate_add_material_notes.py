"""
One-off schema migration: adds a `notes` (description) field to
MaterialPrice, used by _find_or_create_delivery_target's delivery-note
matching (a different description now splits a material into a separate
catalog row; a different brand/manufacturer no longer does).

    python -m migration.migrate_add_material_notes

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE material_price ADD COLUMN IF NOT EXISTS notes VARCHAR(255)
    '''))
    db.session.commit()

print("material_price.notes added (or already existed).")
