"""
One-off schema migration: converts erp_number (on detail, product, and
material_price) from free-text VARCHAR to a unique INTEGER, per the
"ERP № must be a unique integer" requirement. The actual cross-table
uniqueness check lives in app.py's _erp_number_conflict() (one erp_number
can't be reused between a Detail, a Product, and a MaterialPrice either) -
these per-table UNIQUE constraints are just a second line of defense.

Run this once, after migrate_add_detail_label_fields.py and
migrate_add_product_material_label_fields.py have already run:

    python migrate_erp_number_unique_int.py

Only safe to run if no two rows in the same table already share a non-null
erp_number, and every existing value is a plain integer (blank values are
fine, they migrate to NULL).
"""
from sqlalchemy import text

from app import app, db

TABLES = ['detail', 'product', 'material_price']

with app.app_context():
    for table in TABLES:
        db.session.execute(text(f'''
            ALTER TABLE {table}
            ALTER COLUMN erp_number TYPE INTEGER USING NULLIF(erp_number, '')::integer
        '''))
        # Postgres has no "ADD CONSTRAINT IF NOT EXISTS" - swallow the
        # duplicate-object error so this step alone is safe to re-run.
        db.session.execute(text(f'''
            DO $$
            BEGIN
                ALTER TABLE {table} ADD CONSTRAINT {table}_erp_number_unique UNIQUE (erp_number);
            EXCEPTION WHEN duplicate_object THEN
                NULL;
            END $$;
        '''))
    db.session.commit()

print("erp_number columns converted to unique integers.")
