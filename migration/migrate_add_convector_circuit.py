"""
One-off schema migration: which heat pump circuit(s) feed a convector
(convector.heatpump_circuit: NULL = none, 'heating', 'cooling' or 'both') -
drives the heating/cooling pipe layers of the 3D hall map.

    python -m migration.migrate_add_convector_circuit

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE convector ADD COLUMN IF NOT EXISTS heatpump_circuit VARCHAR(10)'))
    db.session.commit()

print('convector.heatpump_circuit added (or already existed).')
