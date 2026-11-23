"""
One-off schema migration: the FSCUT nest file of a cutting-plan sheet (owner's request 2026-11-23).
order_cut_sheet gets nest_file / nest_file_name (VARCHAR(255), NULL = no nest): a nest uploaded for one sheet row of
"Разкрой по листове" fills its counts and gives the sheet's remnant as a DXF / FSCUT plate. See OrderCutSheet in
app.py. Safe to run more than once.

    python -m migration.migrate_add_cut_sheet_nest
"""
from sqlalchemy import text

from app import app, db, OrderCutSheet

with app.app_context():
    OrderCutSheet.__table__.create(db.engine, checkfirst=True)   # sorts before migrate_add_remnant_cut_sheet
    db.session.execute(text('ALTER TABLE order_cut_sheet ADD COLUMN IF NOT EXISTS nest_file VARCHAR(255)'))
    db.session.execute(text('ALTER TABLE order_cut_sheet ADD COLUMN IF NOT EXISTS nest_file_name VARCHAR(255)'))
    db.session.commit()

print('order_cut_sheet.nest_file / nest_file_name added (or already present).')
