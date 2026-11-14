"""
One-off schema migration: adds offer.order_id - the Order created from the offer by admin_offer_create_order()
(see app.py's Offer model; shown on the offers list and in the offer editor). Safe to run more than once.
Existing offers are linked afterwards by migration/data_backfill_offer_orders.py.

    python -m migration.migrate_add_offer_order_id
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE offer
        ADD COLUMN IF NOT EXISTS order_id INTEGER REFERENCES "order"(id)
    '''))
    db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_offer_order_id ON offer (order_id)'))
    db.session.commit()

print("offer.order_id added (or already existed).")
