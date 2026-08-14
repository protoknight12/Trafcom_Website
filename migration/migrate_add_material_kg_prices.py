"""
One-off schema migration: adds material_price.price_per_kg_m2/price_per_kg_m
- alternate weight-based pricing shown/editable for every material type,
informational only (not read by calculate_cnc_price(), which stays on
cost_per_m2 - see the MaterialPrice model docstring). Safe to run more than
once.

    python -m migration.migrate_add_material_kg_prices
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE material_price
        ADD COLUMN IF NOT EXISTS price_per_kg_m2 FLOAT,
        ADD COLUMN IF NOT EXISTS price_per_kg_m FLOAT
    '''))
    db.session.commit()

print("material_price.price_per_kg_m2/price_per_kg_m added (or already existed).")
