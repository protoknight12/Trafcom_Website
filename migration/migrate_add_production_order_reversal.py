"""
One-off schema migration: adds reversed_at/reversed_by_id to production_order,
so deleting a completed job can be soft-deleted (row kept, stock reversed)
instead of hard-deleted - needed for admin_material_history() to show both
the "taken for production" and "returned from production" movements for a
job that got undone. See ProductionOrder/delete_production_order() in
app.py. Safe to run more than once.

    python -m migration.migrate_add_production_order_reversal
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE production_order ADD COLUMN IF NOT EXISTS reversed_at TIMESTAMP
    '''))
    db.session.execute(text('''
        ALTER TABLE production_order ADD COLUMN IF NOT EXISTS reversed_by_id INTEGER
    '''))
    db.session.commit()

print("production_order.reversed_at/reversed_by_id added (or already present).")
