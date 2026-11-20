"""
One-off schema migration: per-piece services (owner's decision 2026-11-20) - service.price_per_piece_eur
(Service.pricing_mode 'piece') and operation.pieces (how many per detail). Safe to run more than once.

    python -m migration.migrate_add_service_piece_pricing
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE service ADD COLUMN IF NOT EXISTS price_per_piece_eur FLOAT'))
    db.session.execute(text('ALTER TABLE operation ADD COLUMN IF NOT EXISTS pieces FLOAT'))
    db.session.commit()

print("service.price_per_piece_eur and operation.pieces added (or already existed).")
