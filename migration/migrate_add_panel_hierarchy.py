"""
One-off schema migration: adds `parent_panel_id`, `overview_pos_x` and
`overview_pos_y` to ElectricalPanel, for panel-to-panel feed links and the
site-wide overview map (see ElectricalPanel's docstring and
admin_factory_map_overview()). This only touches the pre-existing
electrical_panel table.

    python -m migration.migrate_add_panel_hierarchy

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE electrical_panel ADD COLUMN IF NOT EXISTS parent_panel_id INTEGER REFERENCES electrical_panel(id)
    '''))
    db.session.execute(text('''
        ALTER TABLE electrical_panel ADD COLUMN IF NOT EXISTS overview_pos_x DOUBLE PRECISION
    '''))
    db.session.execute(text('''
        ALTER TABLE electrical_panel ADD COLUMN IF NOT EXISTS overview_pos_y DOUBLE PRECISION
    '''))
    db.session.commit()

print("electrical_panel.parent_panel_id / overview_pos_x / overview_pos_y added (or already existed).")
