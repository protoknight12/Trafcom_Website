"""
One-off schema migration: an order-time operation of a per-piece service (owner's decision 2026-11-21) keeps its
count - order_item_operation.pieces (duration_minutes stays 0 for such a row). Safe to run more than once.

    python -m migration.migrate_add_order_item_operation_pieces
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE order_item_operation ADD COLUMN IF NOT EXISTS pieces FLOAT'))
    db.session.commit()

print("order_item_operation.pieces added (or already existed).")
