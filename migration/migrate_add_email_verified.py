"""
One-off schema migration: adds User.email_verified.

Existing accounts are backfilled to TRUE (they're already-established users,
not someone who just typo'd an address at registration) - only new signups
via register()/account_update_email() start out as FALSE and get sent a
verification link. Safe to re-run.

    python -m migration.migrate_add_email_verified
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE "user"
        ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT TRUE
    '''))
    db.session.commit()

print("user.email_verified added (or already existed); existing rows backfilled to TRUE.")
