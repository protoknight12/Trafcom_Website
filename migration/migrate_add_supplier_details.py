"""
Adds the supplier's contact data (SUPPLIER_EXTRA_FIELDS in app.py): address, warehouse_address, mol,
contact_person, contact_phone, phone2, website, bank_account, notes - all optional. Safe to re-run.

    python -m migration.migrate_add_supplier_details
"""
from sqlalchemy import text

from app import app, db

COLUMNS = {
    'address': 'VARCHAR(255)', 'warehouse_address': 'VARCHAR(255)', 'mol': 'VARCHAR(150)',
    'contact_person': 'VARCHAR(150)', 'contact_phone': 'VARCHAR(50)', 'phone2': 'VARCHAR(50)',
    'website': 'VARCHAR(255)', 'bank_account': 'VARCHAR(100)', 'notes': 'TEXT',
}

with app.app_context():
    for column, sql_type in COLUMNS.items():
        db.session.execute(text(f'ALTER TABLE supplier ADD COLUMN IF NOT EXISTS {column} {sql_type}'))
    db.session.commit()

print('supplier contact columns added (or already existed).')
