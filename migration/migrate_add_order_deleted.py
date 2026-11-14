"""
One-off schema migration: adds deleted_at/deleted_by_id to "order", so deleting an
order that already moved stock is a soft delete (row kept, stock reversed, hidden
from every list) - admin_material_history() then still shows both the original
movement and its reversal. See Order/admin_delete_order() in app.py. Safe to run
more than once.

    python -m migration.migrate_add_order_deleted
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE "order" ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP'))
    db.session.execute(text('ALTER TABLE "order" ADD COLUMN IF NOT EXISTS deleted_by_id INTEGER REFERENCES "user"(id)'))
    db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_order_deleted_by_id ON "order" (deleted_by_id)'))
    db.session.commit()

print('order.deleted_at/deleted_by_id added (or already present).')
