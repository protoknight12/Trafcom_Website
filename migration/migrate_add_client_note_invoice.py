"""
One-off schema migration: adds the invoicing fields of a client delivery note (owner's decision 2026-11-20) -
client_delivery_note.invoiced / invoice_number / invoice_date / paid. Existing notes start as not invoiced,
not paid. Safe to run more than once.

    python -m migration.migrate_add_client_note_invoice
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    for column in ('invoiced BOOLEAN NOT NULL DEFAULT FALSE',
                   'invoice_number VARCHAR(100)',
                   'invoice_date DATE',
                   'paid BOOLEAN NOT NULL DEFAULT FALSE'):
        db.session.execute(text(f'ALTER TABLE client_delivery_note ADD COLUMN IF NOT EXISTS {column}'))
    db.session.commit()

print("client_delivery_note invoice columns added (or already existed).")
