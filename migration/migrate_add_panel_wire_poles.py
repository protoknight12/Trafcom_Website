"""
One-off schema migration: adds panel_wire.from_pole/from_side/to_pole/
to_side - which specific numbered pole/terminal (in/out/tap) each end of a
wire connects to, instead of just "this component" as a whole (see
PanelWire's docstring in app.py). Existing wires default to pole 1,
from_side='out'/to_side='in' (the old component-to-component behavior's
closest equivalent).

    python -m migration.migrate_add_panel_wire_poles

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE panel_wire ADD COLUMN IF NOT EXISTS from_pole INTEGER NOT NULL DEFAULT 1
    '''))
    db.session.execute(text('''
        ALTER TABLE panel_wire ADD COLUMN IF NOT EXISTS from_side VARCHAR(3) NOT NULL DEFAULT 'out'
    '''))
    db.session.execute(text('''
        ALTER TABLE panel_wire ADD COLUMN IF NOT EXISTS to_pole INTEGER NOT NULL DEFAULT 1
    '''))
    db.session.execute(text('''
        ALTER TABLE panel_wire ADD COLUMN IF NOT EXISTS to_side VARCHAR(3) NOT NULL DEFAULT 'in'
    '''))
    db.session.commit()

print("panel_wire.from_pole/from_side/to_pole/to_side added (or already existed).")
