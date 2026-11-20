"""
One-off schema migration: detail.external_number - the client's own nomenclature number of a part (owner's
decision 2026-11-20; ours is erp_number, the drawing code_number). Safe to run more than once.

    python -m migration.migrate_add_detail_external_number
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE detail ADD COLUMN IF NOT EXISTS external_number VARCHAR(100)'))
    db.session.commit()

print("detail.external_number added (or already existed).")
