"""
One-off schema migration: batches of identical sheets in a cutting plan ("разкрой по листове").
order_cut_sheet gets copies (INTEGER NOT NULL DEFAULT 1) - one plan row = `copies` identical sheets
with the same pieces each. Existing rows become single sheets. See OrderCutSheet / admin_cut_sheet()
in app.py. Safe to run more than once.

    python -m migration.migrate_add_cut_sheet_copies
"""
from sqlalchemy import text

from app import app, db, OrderCutSheet

with app.app_context():
    OrderCutSheet.__table__.create(db.engine, checkfirst=True)   # sorts before migrate_add_remnant_cut_sheet
    db.session.execute(text('ALTER TABLE order_cut_sheet ADD COLUMN IF NOT EXISTS copies INTEGER NOT NULL DEFAULT 1'))
    db.session.commit()

print('order_cut_sheet.copies added (or already present).')
