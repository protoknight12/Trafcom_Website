"""
One-off schema migration: adds Client/Deliverer support.

Client and Deliverer are brand-new tables, so db.create_all() creates them
automatically (it only refuses to touch tables that already exist) - only
the new client_id/deliverer_id columns on the existing order table need a
manual ALTER. Safe to run more than once.

    python -m migration.migrate_add_client_deliverer
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.create_all()  # creates client / deliverer tables if missing
    db.session.execute(text('''
        ALTER TABLE "order"
        ADD COLUMN IF NOT EXISTS client_id INTEGER REFERENCES client(id),
        ADD COLUMN IF NOT EXISTS deliverer_id INTEGER REFERENCES deliverer(id)
    '''))
    db.session.commit()

print("client/deliverer tables ensured, order.client_id/deliverer_id added (or already existed).")
