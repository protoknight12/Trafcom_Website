"""
One-off schema migration: the outline of a remnant lot (owner's request 2026-11-23). material_price gets
remnant_outline (TEXT, NULL = none): a remnant kept from a cut sheet with an FSCUT nest stores its real shape -
shown as a picture on the material lists and downloaded as a plate for CypCut. See MaterialPrice.remnant_outline
in app.py. Safe to run more than once.

    python -m migration.migrate_add_material_remnant_outline
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE material_price ADD COLUMN IF NOT EXISTS remnant_outline TEXT'))
    db.session.commit()

print('material_price.remnant_outline added (or already present).')
