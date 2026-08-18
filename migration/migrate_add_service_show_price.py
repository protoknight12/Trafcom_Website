"""
One-off schema migration: adds the show_price column to service (see Service
in app.py) - toggles whether a service's hourly rate is shown on the public
/services page. Existing rows backfill to true via the column default, which
is correct - every service's price was shown before this toggle existed.
Safe to run more than once (IF NOT EXISTS). A brand-new database doesn't need
this - db.create_all() already creates the current schema directly. Run this
once with your normal project environment active:

    python -m migration.migrate_add_service_show_price
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE service
        ADD COLUMN IF NOT EXISTS show_price BOOLEAN NOT NULL DEFAULT TRUE
    '''))
    db.session.commit()

print("service.show_price added (or already existed).")
