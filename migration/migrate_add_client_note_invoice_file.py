"""
One-off schema migration: adds the attached invoice PDF of a client delivery note (owner's decision 2026-11-20) -
client_delivery_note.invoice_file (stored name in invoice_files/) and invoice_file_name (the uploaded name).
Safe to run more than once.

    python -m migration.migrate_add_client_note_invoice_file
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    for column in ('invoice_file VARCHAR(255)', 'invoice_file_name VARCHAR(255)'):
        db.session.execute(text(f'ALTER TABLE client_delivery_note ADD COLUMN IF NOT EXISTS {column}'))
    db.session.commit()

print("client_delivery_note invoice file columns added (or already existed).")
