"""
One-off schema migration: adds offer_item.material_id - the material lot a "Материал" offer line sells
(see app.py's OfferItem model and admin_offer_edit.html). Safe to run more than once.

    python -m migration.migrate_add_offer_item_material
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE offer_item
        ADD COLUMN IF NOT EXISTS material_id INTEGER REFERENCES material_price(id)
    '''))
    db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_offer_item_material_id ON offer_item (material_id)'))
    db.session.commit()

print("offer_item.material_id added (or already existed).")
