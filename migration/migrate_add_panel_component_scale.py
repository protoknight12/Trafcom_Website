"""
One-off schema migration: adds panel_component.scale - a per-element icon
size multiplier, independent of every other component's own scale and of
pos_x/pos_y (see PanelComponent's docstring in app.py and the +/- resize
buttons on each card in admin_panel_schematic.html).

    python -m migration.migrate_add_panel_component_scale

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE panel_component ADD COLUMN IF NOT EXISTS scale DOUBLE PRECISION NOT NULL DEFAULT 1.0
    '''))
    db.session.commit()

print("panel_component.scale added (or already existed).")
