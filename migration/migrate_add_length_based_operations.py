"""
One-off schema migration: adds length-based pricing support for Services and
Operations, so a laser cutting operation can be billed per cut length (mm)
instead of per minute - see Service.pricing_mode/price_per_meter_eur and
Operation.length_mm/cost in app.py. Safe to run more than once.

    python -m migration.migrate_add_length_based_operations
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE service ADD COLUMN IF NOT EXISTS pricing_mode VARCHAR(10) NOT NULL DEFAULT 'time'
    '''))
    db.session.execute(text('''
        ALTER TABLE service ADD COLUMN IF NOT EXISTS price_per_meter_eur FLOAT
    '''))
    db.session.execute(text('''
        ALTER TABLE operation ADD COLUMN IF NOT EXISTS length_mm FLOAT
    '''))
    db.session.commit()

print("service.pricing_mode/price_per_meter_eur and operation.length_mm added (or already present).")
