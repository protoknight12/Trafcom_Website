"""
One-off schema migration: adds delivery_note_item.weight_kg - the REAL weight
(kg, from the supplier's invoice) of a whole material line when the user chose
"Реално (по фактура)" on the delivery note; NULL = the computed weight basis
(owner's decision 2026-11-19). Safe to run more than once.

    python -m migration.migrate_add_delivery_item_weight
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE delivery_note_item
        ADD COLUMN IF NOT EXISTS weight_kg FLOAT
    '''))
    db.session.commit()

print("delivery_note_item.weight_kg added (or already existed).")
