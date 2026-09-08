"""
One-off schema migration: adds electrical_panel.schematic_bg_filename/
schematic_bg_scale/schematic_bg_pos_x/schematic_bg_pos_y - the optional
reference photo behind a panel's internal schematic (see
ElectricalPanel's docstring in app.py and /admin/panels/<id>/schematic).

    python -m migration.migrate_add_panel_schematic_background

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE electrical_panel ADD COLUMN IF NOT EXISTS schematic_bg_filename VARCHAR(255)
    '''))
    db.session.execute(text('''
        ALTER TABLE electrical_panel ADD COLUMN IF NOT EXISTS schematic_bg_scale DOUBLE PRECISION NOT NULL DEFAULT 1.0
    '''))
    db.session.execute(text('''
        ALTER TABLE electrical_panel ADD COLUMN IF NOT EXISTS schematic_bg_pos_x DOUBLE PRECISION NOT NULL DEFAULT 50.0
    '''))
    db.session.execute(text('''
        ALTER TABLE electrical_panel ADD COLUMN IF NOT EXISTS schematic_bg_pos_y DOUBLE PRECISION NOT NULL DEFAULT 50.0
    '''))
    db.session.commit()

print("electrical_panel.schematic_bg_* columns added (or already existed).")
