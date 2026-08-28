"""
One-off schema migration: adds material_price.price_per_unit - the price of
one whole stock unit (a full sheet, or one whole rod/pipe/profile length),
informational/catalog-only like price_per_kg_m2/price_per_kg_m, entered on
admin_materials.html and used client-side to auto-fill cost_per_m2. See
MaterialPrice in app.py. Safe to run more than once.

    python -m migration.migrate_add_material_price_per_unit
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE material_price ADD COLUMN IF NOT EXISTS price_per_unit FLOAT
    '''))
    db.session.commit()

print("material_price.price_per_unit added (or already present).")
