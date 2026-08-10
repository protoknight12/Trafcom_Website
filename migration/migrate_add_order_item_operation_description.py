"""
One-off schema migration: adds an optional free-text description column to
order_item_operation, mirroring Operation.description - see
OrderItemOperation in app.py and the per-detail operations picker on
order_create.html. Safe to run more than once.

    python -m migration.migrate_add_order_item_operation_description
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE order_item_operation ADD COLUMN IF NOT EXISTS description VARCHAR(255)
    '''))
    db.session.commit()

print("order_item_operation.description added (or already present).")
