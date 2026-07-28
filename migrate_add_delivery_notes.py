"""
One-off schema migration: adds delivery-note stock intake support.

Supplier / DeliveryNote / DeliveryNoteItem are brand-new tables, so
db.create_all() creates them automatically - only the new stock_quantity
columns on the existing material_price/detail/product tables need a manual
ALTER.

    python migrate_add_delivery_notes.py

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.create_all()  # creates supplier / delivery_note / delivery_note_item tables if missing
    db.session.execute(text('''
        ALTER TABLE material_price ADD COLUMN IF NOT EXISTS stock_quantity DOUBLE PRECISION NOT NULL DEFAULT 0
    '''))
    db.session.execute(text('''
        ALTER TABLE detail ADD COLUMN IF NOT EXISTS stock_quantity DOUBLE PRECISION NOT NULL DEFAULT 0
    '''))
    db.session.execute(text('''
        ALTER TABLE product ADD COLUMN IF NOT EXISTS stock_quantity DOUBLE PRECISION NOT NULL DEFAULT 0
    '''))
    db.session.commit()

print("delivery note tables ensured, stock_quantity added to material_price/detail/product (or already existed).")
