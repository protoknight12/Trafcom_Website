"""
One-off schema migration: adds offer.valid_until - an explicit expiration
date shown on the printed offer (see app.py's Offer model and
templates/admin_offer_print.html), separate from any prose validity mention
in footer_notes. Safe to run more than once.

    python -m migration.migrate_add_offer_valid_until
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE offer
        ADD COLUMN IF NOT EXISTS valid_until DATE
    '''))
    db.session.commit()

print("offer.valid_until added (or already existed).")
