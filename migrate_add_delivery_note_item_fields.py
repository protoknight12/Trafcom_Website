"""
One-off schema migration: adds a free custom description plus width/height/
thickness/brand parameter fields to DeliveryNoteItem, so a delivery note
line can record a batch's actual parameters instead of only its catalog
name/quantity/price.

    python migrate_add_delivery_note_item_fields.py

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE delivery_note_item
        ADD COLUMN IF NOT EXISTS notes VARCHAR(255),
        ADD COLUMN IF NOT EXISTS width DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS height DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS thickness DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS brand VARCHAR(100)
    '''))
    db.session.commit()

print("delivery_note_item.notes/width/height/thickness/brand added (or already existed).")
