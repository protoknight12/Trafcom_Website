"""
One-off schema migration: cutting plans per sheet ("разкрой по листове"). The new tables
order_cut_sheet / order_cut_sheet_item come from db.create_all() (deploy/update.sh runs it
before the migrations; created here too when missing). order_material_remnant gets
cut_sheet_id (a remnant decision for one cut sheet), and its one-decision-per-(order, lot)
constraint becomes a partial unique index for the group-level decisions only (cut_sheet_id
IS NULL) plus a unique index on cut_sheet_id. See OrderCutSheet / admin_cut_sheet() in
app.py. Safe to run more than once.

    python -m migration.migrate_add_remnant_cut_sheet
"""
from sqlalchemy import text

from app import app, db, OrderCutSheet, OrderCutSheetItem

with app.app_context():
    OrderCutSheet.__table__.create(db.engine, checkfirst=True)
    OrderCutSheetItem.__table__.create(db.engine, checkfirst=True)
    db.session.execute(text('ALTER TABLE order_material_remnant '
                            'ADD COLUMN IF NOT EXISTS cut_sheet_id INTEGER REFERENCES order_cut_sheet(id)'))
    db.session.execute(text('ALTER TABLE order_material_remnant DROP CONSTRAINT IF EXISTS uq_order_material_remnant'))
    db.session.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_order_material_remnant_group '
                            'ON order_material_remnant (order_id, material_id) WHERE cut_sheet_id IS NULL'))
    db.session.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS order_material_remnant_cut_sheet_id_key '
                            'ON order_material_remnant (cut_sheet_id)'))
    db.session.commit()

print('order_material_remnant.cut_sheet_id added (or already present).')
