"""
One-off schema migration: adds quarterly ГО installment tracking to Vehicle -
insurance_installments (new boolean, default False - matches every existing
row's original single-annual-payment behavior) plus the new
vehicle_insurance_installment table (created via db.create_all(), tracks the
'paid' checkbox per quarterly due date - see Vehicle.insurance_installment_dates/
next_insurance_installment and VehicleInsuranceInstallment in app.py).

    python -m migration.migrate_add_vehicle_insurance_installments

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE vehicle ADD COLUMN IF NOT EXISTS insurance_installments BOOLEAN NOT NULL DEFAULT FALSE
    '''))
    db.session.commit()
    db.create_all()

print("vehicle.insurance_installments added, vehicle_insurance_installment table created (or already done).")
