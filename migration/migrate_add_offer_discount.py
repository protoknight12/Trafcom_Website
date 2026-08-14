"""
One-off schema migration: adds offer.discount_percent - a whole-offer
discount (0-100) applied to the item subtotal, shown on the printed offer as
"price; -sale%, actual price" (see app.py's Offer.subtotal/discount_amount/
total properties and templates/admin_offer_print.html). Safe to run more
than once.

    python -m migration.migrate_add_offer_discount
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE offer
        ADD COLUMN IF NOT EXISTS discount_percent FLOAT
    '''))
    db.session.commit()

print("offer.discount_percent added (or already existed).")
