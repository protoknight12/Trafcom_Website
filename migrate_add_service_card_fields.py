"""
One-off schema migration: adds section_title/specs_text/image_filename to the
service_machine_card table (see ServiceMachineCard in app.py) - these were added
when the table grew from "web-designer-added text cards only" to also backing the
migrated-from-hardcoded-HTML machine park on the services page. Safe to run more
than once (IF NOT EXISTS). A brand-new database doesn't need this - db.create_all()
already creates the current schema directly. Run this once with your normal project
environment active:

    python migrate_add_service_card_fields.py
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE service_machine_card
        ADD COLUMN IF NOT EXISTS section_title VARCHAR(150),
        ADD COLUMN IF NOT EXISTS specs_text TEXT,
        ADD COLUMN IF NOT EXISTS image_filename VARCHAR(255)
    '''))
    db.session.commit()

print("service_machine_card columns added (or already existed).")
