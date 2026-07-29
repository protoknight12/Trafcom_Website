"""
One-off schema migration: adds the page column to service_machine_card (see
ServiceMachineCard in app.py) - scopes cards to 'services' or 'index' now that the
homepage also has DB-backed machine cards. Existing rows backfill to 'services' via
the column default, which is correct - they were the only page these cards lived on
before this migration. Safe to run more than once (IF NOT EXISTS). A brand-new
database doesn't need this - db.create_all() already creates the current schema
directly. Run this once with your normal project environment active:

    python -m migration.migrate_add_service_card_page
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE service_machine_card
        ADD COLUMN IF NOT EXISTS page VARCHAR(20) NOT NULL DEFAULT 'services'
    '''))
    db.session.commit()

print("service_machine_card.page added (or already existed).")
