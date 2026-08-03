"""
One-off schema migration: adds the kind column to service_machine_card (see
ServiceMachineCard in app.py) - distinguishes machine cards from the new
Products section cards on the services page. Existing rows backfill to
'machine' via the column default, which is correct - every card that existed
before this migration was a machine card. Safe to run more than once
(IF NOT EXISTS). A brand-new database doesn't need this - db.create_all()
already creates the current schema directly. Run this once with your normal
project environment active:

    python -m migration.migrate_add_service_card_kind
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE service_machine_card
        ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'machine'
    '''))
    db.session.commit()

print("service_machine_card.kind added (or already existed).")
