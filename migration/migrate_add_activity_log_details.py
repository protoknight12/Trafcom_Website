"""
One-off schema migration: adds an optional long-text `details` column to
activity_log, so the admin log dashboard can show a full multi-line
breakdown (e.g. every line of a delivery note or order, not just the
one-line action summary) when a row is expanded - see ActivityLog and
log_action()'s details= param in app.py. Safe to run more than once.

    python -m migration.migrate_add_activity_log_details
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS details TEXT
    '''))
    db.session.commit()

print("activity_log.details added (or already present).")
