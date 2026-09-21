"""
One-off schema migration: adds per-client discount/markup pricing.

    python -m migration.migrate_add_client_pricing

- user.client_id: which Client a login is linked to (drives its pricing).
- client.material_adjustment_type/percent, client.detail_adjustment_type/percent:
  default discount/markup per category (Материали, Детайли).
- client_service_price: per-(client, service) rate override.

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE "user" ADD COLUMN IF NOT EXISTS client_id INTEGER REFERENCES client(id)
    '''))
    db.session.execute(text('''
        ALTER TABLE client ADD COLUMN IF NOT EXISTS material_adjustment_type VARCHAR(10)
    '''))
    db.session.execute(text('''
        ALTER TABLE client ADD COLUMN IF NOT EXISTS material_adjustment_percent FLOAT
    '''))
    db.session.execute(text('''
        ALTER TABLE client ADD COLUMN IF NOT EXISTS detail_adjustment_type VARCHAR(10)
    '''))
    db.session.execute(text('''
        ALTER TABLE client ADD COLUMN IF NOT EXISTS detail_adjustment_percent FLOAT
    '''))
    db.session.execute(text('''
        CREATE TABLE IF NOT EXISTS client_service_price (
            id SERIAL PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES client(id),
            service_id INTEGER NOT NULL REFERENCES service(id),
            adjustment_type VARCHAR(10),
            adjustment_percent FLOAT,
            UNIQUE(client_id, service_id)
        )
    '''))
    db.session.commit()

print("user.client_id, client pricing columns, and client_service_price added (or already existed).")
