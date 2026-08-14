"""
One-off schema migration: adds material_price.weight_kg - the actual
measured weight (kg) of one stock unit (a full sheet, or one linear meter
for rods/pipes/profiles), informational only like price_per_kg_m2/
price_per_kg_m (see the MaterialPrice model docstring in app.py). Safe to
run more than once.

    python -m migration.migrate_add_material_weight
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE material_price
        ADD COLUMN IF NOT EXISTS weight_kg FLOAT
    '''))
    db.session.commit()

print("material_price.weight_kg added (or already existed).")
