"""
One-off schema migration (owner, 2026-11-24): machine_cut_technology.needs_test - a proposed technology that has not
passed a test cut yet; "Работи ✓" in the machine's dossier clears it. Existing rows are tested (false). Safe to run
more than once.

    python -m migration.migrate_add_cut_tech_needs_test
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE machine_cut_technology '
                            'ADD COLUMN IF NOT EXISTS needs_test BOOLEAN NOT NULL DEFAULT FALSE'))
    db.session.commit()

print('machine_cut_technology.needs_test added (or already existed).')
