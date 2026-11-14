"""
One-off schema migration: adds material_request.note - why a restock request was raised, e.g. the order
whose material reservation fell short (app.py's _reserve_order_material() writes "Поръчка <номер>").
Requests raised by hand keep NULL. The new order_material_reservation table needs nothing here -
db.create_all() creates it. Safe to run more than once.

    python -m migration.migrate_add_material_request_note
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE material_request ADD COLUMN IF NOT EXISTS note VARCHAR(255)'))
    db.session.commit()

print("material_request.note added (or already existed).")
