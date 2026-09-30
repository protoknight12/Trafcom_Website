"""
One-off schema migration: per-client access limits (apps, machines,
services, public details/products) - see client_allows().

    python -m migration.migrate_add_client_access

client.access_json NULL = unrestricted, so existing clients keep full access.
Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE client ADD COLUMN IF NOT EXISTS access_json TEXT'))
    db.session.commit()

print('client.access_json added (or already existed).')
