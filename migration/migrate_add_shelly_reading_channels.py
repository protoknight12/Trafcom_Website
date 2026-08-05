"""
One-off schema migration: adds channels_json to shelly_reading_log (see
ShellyReadingLog in app.py) - stores each poll tick's raw per-phase
voltage/current/power breakdown (json.dumps'd list), not just the
total_power/total_energy columns that existed before this. Existing rows
backfill to NULL, which _aggregate_local_shelly_log() already treats as "no
per-phase data for this row" rather than an error. Safe to run more than
once (IF NOT EXISTS). A brand-new database doesn't need this -
db.create_all() already creates the current schema directly. Run this once
with your normal project environment active:

    python -m migration.migrate_add_shelly_reading_channels
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE shelly_reading_log
        ADD COLUMN IF NOT EXISTS channels_json TEXT
    '''))
    db.session.commit()

print("shelly_reading_log.channels_json added (or already existed).")
