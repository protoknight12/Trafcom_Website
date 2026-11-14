"""
One-off schema migration: order.issued_note_id - the ClientDeliveryNote that "Изпиши към клиента" (admin_order_issue())
issued for a completed order (+ index). NULL = not issued yet.

    python -m migration.migrate_add_order_issued_note

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE "order" ADD COLUMN IF NOT EXISTS issued_note_id INTEGER REFERENCES client_delivery_note(id)'))
    db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_order_issued_note_id ON "order"(issued_note_id)'))
    db.session.commit()

print('order.issued_note_id is ready.')
