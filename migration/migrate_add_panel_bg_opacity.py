"""
One-off schema migration: adds electrical_panel.schematic_bg_opacity - how
much the reference photo backdrop shows through under the drawn schematic
(see ElectricalPanel's docstring in app.py and the opacity slider next to
the background's scale input in admin_panel_schematic.html). Explicit ask:
"да има и прозрачност на подложката".

    python -m migration.migrate_add_panel_bg_opacity

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE electrical_panel ADD COLUMN IF NOT EXISTS schematic_bg_opacity DOUBLE PRECISION NOT NULL DEFAULT 0.85
    '''))
    db.session.commit()

print("electrical_panel.schematic_bg_opacity added (or already existed).")
