"""
One-off schema migration: adds an optional free-text description column to
operation, so an admin can note what an operation actually was beyond its
Service name (e.g. "лазерно рязане" done in-house vs. "външно лазерно рязане"
outsourced elsewhere) - see Operation in app.py and the operations section on
detail_dxf_dashboard.html. Safe to run more than once.

    python -m migration.migrate_add_operation_description
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE operation ADD COLUMN IF NOT EXISTS description VARCHAR(255)
    '''))
    db.session.commit()

print("operation.description added (or already present).")
