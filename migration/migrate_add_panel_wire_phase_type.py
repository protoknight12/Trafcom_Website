"""
One-off schema migration: adds panel_wire.phase_type ('three_phase' or
'single_phase' - see PANEL_WIRE_PHASE_TYPES/PanelWire's docstring in
app.py), defaulting every existing wire to 'three_phase'.

    python -m migration.migrate_add_panel_wire_phase_type

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE panel_wire ADD COLUMN IF NOT EXISTS phase_type VARCHAR(20) NOT NULL DEFAULT 'three_phase'
    '''))
    db.session.commit()

print("panel_wire.phase_type added (or already existed).")
