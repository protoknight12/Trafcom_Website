"""
One-off schema migration: adds material_request.quoted_unit_price and quoted_at - a supplier's quote on a restock
request (status 'quoted' "Оферирана", kept while 'ordered'): € per ONE stock unit of the lot. While the lot has no
stock, app.py's material_cost_rate() prices its details from the latest quote (owner's request 2026-11-23).
Existing requests keep NULL (no quote). Safe to run more than once.

    python -m migration.migrate_add_material_request_quote
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE material_request ADD COLUMN IF NOT EXISTS quoted_unit_price DOUBLE PRECISION'))
    db.session.execute(text('ALTER TABLE material_request ADD COLUMN IF NOT EXISTS quoted_at TIMESTAMP'))
    db.session.commit()

print("material_request.quoted_unit_price / quoted_at added (or already existed).")
