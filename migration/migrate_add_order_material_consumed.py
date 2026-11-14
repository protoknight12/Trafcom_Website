"""
One-off schema migration: adds material_consumed + consumed_material_id to order_item and
order_item_component - the raw material production by order took from stock and the lot it came
from (see app.py's _consume_detail_material(); shown in admin_material_history()). Production
recorded before this keeps 0 / NULL: it never moved material. Safe to run more than once.

    python -m migration.migrate_add_order_material_consumed
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    for table in ('order_item', 'order_item_component'):
        db.session.execute(text(f'''
            ALTER TABLE {table}
            ADD COLUMN IF NOT EXISTS material_consumed DOUBLE PRECISION NOT NULL DEFAULT 0
        '''))
        db.session.execute(text(f'''
            ALTER TABLE {table}
            ADD COLUMN IF NOT EXISTS consumed_material_id INTEGER REFERENCES material_price(id)
        '''))
        db.session.execute(text(f'CREATE INDEX IF NOT EXISTS ix_{table}_consumed_material_id '
                                f'ON {table} (consumed_material_id)'))
    db.session.commit()

print("order_item / order_item_component material_consumed + consumed_material_id added (or already existed).")