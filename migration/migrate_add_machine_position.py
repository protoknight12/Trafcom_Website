"""
One-off schema migration: adds `pos_x`/`pos_y` to Machine (interactive
factory map at /admin/factory-map - see admin_factory_map()). This only
touches the pre-existing machine table.

    python -m migration.migrate_add_machine_position

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE machine ADD COLUMN IF NOT EXISTS pos_x DOUBLE PRECISION
    '''))
    db.session.execute(text('''
        ALTER TABLE machine ADD COLUMN IF NOT EXISTS pos_y DOUBLE PRECISION
    '''))
    db.session.commit()

print("machine.pos_x / machine.pos_y added (or already existed).")
