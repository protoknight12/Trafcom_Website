"""
One-off schema migration: adds an `iso8015` boolean column to QualityCheck -
whether the inspection was carried out under the ISO 8015 independency
principle (a reference/interpretation flag only, ISO 8015 has no tolerance
values of its own - see admin_quality_control.html's checkbox).

    python -m migration.migrate_add_quality_check_iso8015

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE quality_check ADD COLUMN IF NOT EXISTS iso8015 BOOLEAN NOT NULL DEFAULT FALSE
    '''))
    db.session.commit()

print("quality_check.iso8015 added (or already existed).")
