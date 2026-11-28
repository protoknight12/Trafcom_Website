"""
One-off schema migration: thermostat.guard_temp - the heating threshold a DISABLED thermostat still keeps
(its convectors switch on below it). NULL = no threshold. Safe to run more than once.

    python -m migration.migrate_add_thermostat_guard
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE thermostat ADD COLUMN IF NOT EXISTS guard_temp FLOAT'))
    db.session.commit()

print('thermostat.guard_temp added (or already existed).')
