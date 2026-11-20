"""
One-off schema migration: the client's request file of an order (owner's decision 2026-11-20) -
order.request_file (stored name in order_item_files/) and request_file_name (the uploaded name).
Safe to run more than once.

    python -m migration.migrate_add_order_request_file
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    for column in ('request_file VARCHAR(255)', 'request_file_name VARCHAR(255)'):
        db.session.execute(text(f'ALTER TABLE "order" ADD COLUMN IF NOT EXISTS {column}'))
    db.session.commit()

print('order.request_file / request_file_name added (or already existed).')
