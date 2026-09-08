"""
One-off schema migration: creates the SolisReadingLog table (new, via
db.create_all() - see SolisReadingLog in app.py). Starts filling itself once
the app is restarted and start_solis_history_poller() begins running -
nothing to backfill.

    python -m migration.seed_solis_reading_log

Safe to run more than once.
"""
from app import app, db

with app.app_context():
    db.create_all()

print("SolisReadingLog table created (or already existed).")
