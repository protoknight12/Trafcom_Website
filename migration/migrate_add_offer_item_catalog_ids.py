"""
One-off schema migration: adds product_id/detail_id FK columns to
OfferItem, so a catalog-picked offer line can be traced back to the real
Product/Detail row it came from (needed by admin_offer_create_order() to
turn selected offer lines into a real Order). Existing rows are left with
both columns NULL - they can still be viewed/printed, just not selected for
order creation until the offer is re-saved.

    python -m migration.migrate_add_offer_item_catalog_ids

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE offer_item ADD COLUMN IF NOT EXISTS product_id INTEGER REFERENCES product(id)
    '''))
    db.session.execute(text('''
        ALTER TABLE offer_item ADD COLUMN IF NOT EXISTS detail_id INTEGER REFERENCES detail(id)
    '''))
    db.session.commit()

print("offer_item.product_id / offer_item.detail_id added (or already existed).")
