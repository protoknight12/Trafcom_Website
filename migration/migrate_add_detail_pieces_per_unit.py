"""
One-off schema migration: detail.pieces_per_unit - "Детайли от една единица" (owner's decision 2026-11-21): how many
of a detail come out of one sheet / bar / piece of its material; NULL = off (bounding box). Safe to run more than once.

    python -m migration.migrate_add_detail_pieces_per_unit
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE detail ADD COLUMN IF NOT EXISTS pieces_per_unit INTEGER'))
    db.session.commit()

print("detail.pieces_per_unit added (or already existed).")
